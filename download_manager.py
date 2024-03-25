import datetime
import fnmatch
import logging
import os
import re
import shutil
import time
import urllib
import zipfile
from io import BytesIO
from urllib.error import HTTPError
from goes2go import GOES

import eumdac
import fsspec
import requests
import structlog

import warnings

# Suppress FutureWarning related to 'H' argument
warnings.filterwarnings('ignore', category=FutureWarning)

from satip import utils
from satip.data_store import dateset_it_to_filename

log = structlog.stdlib.get_logger()

# constants for different data sources
EUMETSAT_PROVIDER = "EUMETSAT"
GOES_PROVIDER = "GOES"



API_ENDPOINT = "https://api.eumetsat.int"

# Data Store searching endpoint
API_SEARCH_ENDPOINT = API_ENDPOINT + "/data/search-products/os"

# Data Tailor customisations endpoint
API_CUSTOMIZATION_ENDPOINT = API_ENDPOINT + "/epcs/customisations"

# Data Tailor download endpoint
API_TAILORED_DOWNLOAD_ENDPOINT = API_ENDPOINT + "/epcs/download"

# Data Tailor time out
DATA_TAILOR_TIMEOUT_LIMIT_MINUTES = 15


def _request_access_token(user_key, user_secret):
    """
    Requests an access token from the EUMETSAT data API

    Args:
        user_key: EUMETSAT API key
        user_secret: EUMETSAT API secret

    Returns:
        access_token: API access token

    """

    token_url = "https://api.eumetsat.int/token"

    r = requests.post(
        token_url,
        auth=requests.auth.HTTPBasicAuth(user_key, user_secret),
        data={"grant_type": "client_credentials"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    access_token = r.json()["access_token"]

    return access_token


def query_data_products(
    start_date: str = "2020-01-01",
    end_date: str = "2020-01-02",
    start_index: int = 0,
    num_features: int = 10_000,
    product_id: str = "EO:EUM:DAT:MSG:MSG15-RSS",
) -> requests.models.Response:
    """Queries the EUMETSAT-API for the specified product and date-range.

    Queries the EUMETSAT data API for the specified data
    product and date-range. The dates will accept any
    format that can be interpreted by `pd.to_datetime`.
    A maximum of 10,000 entries are returned by the API
    so the indexes of the returned entries can be specified.

    Args:
        start_date: Start of the query period
        end_date: End of the query period
        start_index: Starting index of returned entries
        num_features: Number of returned entries
        product_id: ID of the EUMETSAT product requested

    Returns:
        r: Response from the request
    """

    search_url = API_ENDPOINT + "/data/search-products/1.0.0/os"

    params = {
        "format": "json",
        "pi": product_id,
        "si": start_index,
        "c": num_features,
        "sort": "start,time,0",
        "dtstart": utils.format_dt_str(start_date),
        "dtend": utils.format_dt_str(end_date),
    }

    r = requests.get(search_url, params=params)
    r.raise_for_status()

    return r


def identify_available_datasets(
    start_date: str, end_date: str, product_id: str = "EO:EUM:DAT:MSG:MSG15-RSS"
):
    """Identifies available datasets from the EUMETSAT data API

    Identified available dataset for the specified data product and date-range.
    The dates will accept any format that can be interpreted by `pd.to_datetime`.

    Args:
        start_date: Start of the query period
        end_date: End of the query period
        product_id: ID of the EUMETSAT product requested
        log: logger to send log messages to, set to None for no logging

    Returns:
        JSON-formatted response from the request
    """
    log.info(
        f"Identifying which dataset are available for {start_date} {end_date} {product_id}",
        productID=product_id,
    )

    r_json = query_data_products(start_date, end_date, product_id=product_id).json()

    num_total_results = r_json["totalResults"]
    if log:
        log.info(f"Found {num_total_results} EUMETSAT dataset files", productID=product_id)

    if num_total_results < 500:
        return r_json["features"]

    datasets = r_json["features"]

    # need to loop in batches of 10_000 until all results are found
    extra_loops_needed = num_total_results // 500

    new_end_date = datasets[-1]["properties"]["date"].split("/")[1]

    for i in range(extra_loops_needed):
        # ensure the last loop we only get the remaining assets
        if i + 1 < extra_loops_needed:
            num_features = 500
        else:
            num_features = num_total_results - len(datasets)

        batch_r_json = query_data_products(
            start_date, new_end_date, num_features=num_features, product_id=product_id
        ).json()
        new_end_date = batch_r_json["features"][-1]["properties"]["date"].split("/")[1]
        datasets = datasets + batch_r_json["features"]

    if num_total_results != len(datasets):
        log.warn(
            f"Some features have not been appended - {len(datasets)} / {num_total_results}",
            productID=product_id,
        )

    return datasets


# TODO: Passing the access token is redundant, as we call the API with the token in params-arg.
def dataset_id_to_link(collection_id, data_id, access_token):
    """Generates a link for the get request.

    Args:
        collection_id: ID of the collection to request from.
        data_id: Product ID to request for.
        access_token: Access token for the request.

    Returns:
        str containing the URL for the dataset request.
    """
    return (
        "https://api.eumetsat.int/data/download/1.0.0/collections/"
        + f"{urllib.parse.quote(collection_id)}/products/{urllib.parse.quote(data_id)}"
        + "?access_token="
        + access_token
    )



class EUMETSATDownloadManager:
    """
    Manager class for downloading EUMETSAT data.
    """

    def __init__(
        self,
        user_key: str,
        user_secret: str,
        data_dir: str,
        native_file_dir: str = ".",
    ):
        """Download manager initialisation

        Initialises the download manager by:
        * Requesting an API access token
        * Configuring the download directory
        * Adding satip helper functions

        Args:
            user_key: EUMETSAT API key
            user_secret: EUMETSAT API secret
            data_dir: Path to the directory where the satellite data will be saved
            native_file_dir: this is where the native files are saved

        Returns:
            download_manager: Instance of the DownloadManager class
        """

        # Requesting the API access token
        self.user_key = user_key
        self.user_secret = user_secret

        self.request_access_token()

        # Configuring the data directory
        self.data_dir = data_dir
        self.native_file_dir = native_file_dir

        if not os.path.exists(self.data_dir):
            try:
                os.makedirs(self.data_dir)
            except PermissionError:
                raise PermissionError(f"No permission to create {self.data_dir}.")

        # Adding satip helper functions
        self.identify_available_datasets = identify_available_datasets
        self.query_data_products = query_data_products

        return

    def request_access_token(self, user_key=None, user_secret=None):
        """Requests an access token from the EUMETSAT data API.

        If no key or secret are provided then they will default
        to the values provided in the download manager initialisation.

        The requested token is stored in the respective class field.

        Args:
            user_key: EUMETSAT API key
            user_secret: EUMETSAT API secret
        """

        if user_key is None:
            user_key = self.user_key
        if user_secret is None:
            user_secret = self.user_secret

        self.access_token = _request_access_token(user_key, user_secret)

        return

    def download_single_dataset(self, data_link: str):
        """Downloads a single dataset from the EUMETSAT API

        Args:
            data_link: Url link for the relevant dataset
        """

        log.info(f"Downloading one file: {data_link}", parent="DownloadManager")

        params = {"access_token": self.access_token}

        r = requests.get(data_link, params=params)
        r.raise_for_status()

        zipped_files = zipfile.ZipFile(BytesIO(r.content))
        zipped_files.extractall(f"{self.data_dir}")

        return

    def download_date_range(
        self, start_date: str, end_date: str, product_id="EO:EUM:DAT:MSG:MSG15-RSS"
    ):
        """Downloads a date-range-specific dataset from the EUMETSAT API

        Args:
            start_date: Start of the requested data period
            end_date: End of the requested data period
            product_id: ID of the EUMETSAT product requested
        """

        datasets = identify_available_datasets(start_date, end_date, product_id=product_id)
        self.download_datasets(datasets, product_id=product_id)

    def download_datasets(self, datasets, product_id="EO:EUM:DAT:MSG:MSG15-RSS"):
        """Downloads a product-id- and date-range-specific dataset from the EUMETSAT API

        Args:
            datasets: list of datasets returned by `identify_available_datasets`
            product_id: ID of the EUMETSAT product requested
        """

        # Identifying dataset ids to download
        dataset_ids = sorted([dataset["id"] for dataset in datasets])

        # Downloading specified datasets
        if not dataset_ids:
            log.info(
                "No files will be downloaded. None were found in API search.",
                parent="DownloadManager",
            )
            return

        for dataset_id in dataset_ids:
            log.debug(f"Downloading: {dataset_id}", parent="DownloadManager")
            dataset_link = dataset_id_to_link(
                product_id, dataset_id, access_token=self.access_token
            )
            # Download the raw data
            try:
                self.download_single_dataset(dataset_link)
            except HTTPError:
                log.debug("The EUMETSAT access token has been refreshed", parent="DownloadManager")
                self.request_access_token()
                dataset_link = dataset_id_to_link(
                    product_id, dataset_id, access_token=self.access_token
                )
                self.download_single_dataset(dataset_link)
            except Exception as e:
                log.error(
                    f"Error downloading dataset with id {dataset_id}: {e}",
                    exc_info=True,
                    parent="DownloadManager",
                )

    def download_tailored_date_range(
        self,
        start_date: str,
        end_date: str,
        product_id="EO:EUM:DAT:MSG:MSG15-RSS",
        roi: str = "united_kingdom",
        file_format: str = "hrit",
        projection: str = "geographic",
    ):
        """Downloads a set of tailored datasets from the EUMETSAT API

        Datasets will be in the defined date range and from the specified product
        using the Data Tailor API.

        Args:
            start_date: Start of the requested data period
            end_date: End of the requested data period
            product_id: ID of the EUMETSAT product requested
            roi: Region of interest, None if you want the whole original area
            file_format: File format to request, multiple options, primarily 'netcdf4' and 'geotiff'
            projection: Projection of the stored data, defaults to 'geographic'
        """

        datasets = identify_available_datasets(start_date, end_date, product_id=product_id)
        self.download_tailored_datasets(
            datasets, product_id=product_id, file_format=file_format, projection=projection, roi=roi
        )

    def download_tailored_datasets(
        self,
        datasets,
        product_id: str = "EO:EUM:DAT:MSG:MSG15-RSS",
        roi: str = None,
        file_format: str = "hrit",
        projection: str = None,
    ):
        """
        Query the data tailor service and write the requested ROI data to disk

        Args:
            datasets: Dataset to extract ids from, for which the tailored sets will be downloaded
            product_id: Product ID for the Data Store
            roi: Region of Interest, None if want the whole original area
            file_format: File format to request, multiple options, primarily 'netcdf4' and 'geotiff'
            projection: Projection of the stored data, defaults to 'geographic'
        """

        # Identifying dataset ids to download
        dataset_ids = sorted([dataset["id"] for dataset in datasets])
        log.debug(f"Dataset IDS: {dataset_ids}", parent="DownloadManager")
        # Downloading specified datasets
        if not dataset_ids:
            log.info(
                "No files will be downloaded. None were found in API search.",
                parent="DownloadManager",
            )
            return

        for dataset_id in dataset_ids:
            # Download the raw data
            try:
                self._download_single_tailored_dataset(
                    dataset_id,
                    product_id=product_id,
                    roi=roi,
                    file_format=file_format,
                    projection=projection,
                )
            except Exception:
                log.debug("The EUMETSAT access token has been refreshed", parent="DownloadManager")
                self.request_access_token()
                self._download_single_tailored_dataset(
                    dataset_id,
                    product_id=product_id,
                    roi=roi,
                    file_format=file_format,
                    projection=projection,
                )

    def _download_single_tailored_dataset(
        self,
        dataset_id,
        product_id: str = "EO:EUM:DAT:MSG:MSG15-RSS",
        roi: str = None,
        file_format: str = "hrit",
        projection: str = None,
    ):
        """
        Download a single tailored dataset

        Args:
            dataset_id: Dataset ID to download
            product_id: Product ID to determine the ID for the request
            roi: Region of Interest for the area, if None, then no cropping is done
            file_format: File format of the output, defaults to 'geotiff'
            projection: Projection for the output, defaults to native projection of 'geographic'

        return string where the dataset has been saved
        """

        SEVIRI = "HRSEVIRI"
        SEVIRI_HRV = "HRSEVIRI_HRV"
        RSS_ID = "HRSEVIRI_RSS"
        CLM_ID = "MSGCLMK"

        if product_id == "EO:EUM:DAT:MSG:MSG15-RSS":
            tailor_id = RSS_ID
        elif product_id == "EO:EUM:DAT:MSG:MSG15":
            tailor_id = SEVIRI
        elif product_id == "EO:EUM:DAT:MSG:HRSEVIRI":
            tailor_id = SEVIRI
        elif product_id == "EO:EUM:DAT:MSG:RSS-CLM":
            tailor_id = CLM_ID
        else:
            raise ValueError(f"Product ID {product_id} not recognized, ending now")

        if tailor_id == SEVIRI:  # Also do HRV
            credentials = (self.user_key, self.user_secret)
            token = eumdac.AccessToken(credentials)
            datastore = eumdac.DataStore(token)
            product_id = datastore.get_product("EO:EUM:DAT:MSG:HRSEVIRI", dataset_id)
            self.create_and_download_datatailor_data(
                dataset_id=product_id,
                tailor_id=SEVIRI_HRV,
                roi=roi,
                file_format=file_format,
                projection=projection,
            )

        credentials = (self.user_key, self.user_secret)
        token = eumdac.AccessToken(credentials)
        datastore = eumdac.DataStore(token)
        product_id = datastore.get_product("EO:EUM:DAT:MSG:HRSEVIRI", dataset_id)
        self.create_and_download_datatailor_data(
            dataset_id=product_id,
            tailor_id=tailor_id,
            roi=roi,
            file_format=file_format,
            projection=projection,
        )

    def cleanup_datatailor(self):
        """Remove all Data Tailor runs"""
        credentials = (self.user_key, self.user_secret)
        token = eumdac.AccessToken(credentials)
        datatailor = eumdac.DataTailor(token)
        for customisation in datatailor.customisations:
            try:
                if customisation.status in ['INACTIVE']:
                    customisation.kill()
                if customisation.status in ['DONE', 'FAILED', 'KILLED', 'DELETED']:
                    log.debug(
                        f"Delete completed customisation {customisation} "
                        f"from {customisation.creation_time}."
                    )
                    customisation.delete()
            except Exception as e:
                log.debug(f"Failed customization delete because of: {e}")
    def create_and_download_datatailor_data(
        self,
        dataset_id,
        tailor_id: str = "HRSEVIRI",
        roi: str = None,
        file_format: str = "hrit",
        projection: str = None,
        compression: dict = {"format": "zip"},
    ):
        """
        Create and download a single data tailor call
        """

        # check data store, if its there use this instead
        data_store_filename_remote = dateset_it_to_filename(
            dataset_id, tailor_id, self.native_file_dir
        )
        data_store_filename_local = dateset_it_to_filename(dataset_id, tailor_id, self.data_dir)

        fs = fsspec.open(data_store_filename_remote).fs
        if fs.exists(data_store_filename_remote):
            # copy to 'data_dir'
            log.debug(
                f"Copying file from {data_store_filename_remote} to {data_store_filename_local}",
                parent="DownloadManager",
            )
            fs.get(data_store_filename_remote, data_store_filename_local)

        else:
            log.debug(
                f"{data_store_filename_remote} does not exist, so will download it",
                parent="DownloadManager",
            )

            log.debug("Making customisation, this can take ~1 minute", parent="DownloadManager")
            chain = eumdac.tailor_models.Chain(
                product=tailor_id,
                format=file_format,
                projection=projection,
                roi=roi,
                compression=compression,
            )

            datatailor = eumdac.DataTailor(eumdac.AccessToken((self.user_key, self.user_secret)))

            # sometimes the customisation fails first time, so we try twice
            # This is from Data Tailor only allowing 3 customizations at once
            # So this should then continue until it is created successfully
            created_customization = False
            # 5 minute timeout here
            start = datetime.datetime.now()
            while not created_customization and (datetime.datetime.now() - start).seconds < 600:
                try:
                    num_running_customizations = 0
                    for customisation in datatailor.customisations:
                        if customisation.status in ['INACTIVE']: # Clear stuck ones
                            customisation.kill()
                            customisation.delete()
                        if customisation.status in ['RUNNING','QUEUED', 'INACTIVE']:
                            num_running_customizations += 1
                    if num_running_customizations < 3:
                        customisation = datatailor.new_customisation(dataset_id, chain=chain)
                        created_customization = True
                except Exception:
                    log.debug("Customization not made successfully, so "
                              "trying again after less than 3 customizations")
                    time.sleep(3)
                    continue

            sleep_time = 5  # seconds
            log.debug(f"Customisation: {customisation}", parent="DownloadManager")
            # Customisation Loop
            now = datetime.datetime.now(tz=datetime.timezone.utc)
            start = datetime.datetime.now(tz=datetime.timezone.utc)
            status = datatailor.get_customisation(customisation._id).status
            while (status != "DONE") & (
                now - start < datetime.timedelta(minutes=DATA_TAILOR_TIMEOUT_LIMIT_MINUTES)
            ):

                log.debug(
                    f"Checking if the file has been downloaded. Started at {start}. "
                    f"Time out is {DATA_TAILOR_TIMEOUT_LIMIT_MINUTES} minutes",
                    parent="DownloadManager",
                )

                # Get the status of the ongoing customisation
                status = datatailor.get_customisation(customisation._id).status
                now = datetime.datetime.now(tz=datetime.timezone.utc)
                log.info(f"Status of ID {customisation._id} is {status}", parent="DownloadManager")

                if "DONE" == status:
                    break
                elif "ERROR" in status or "KILLED" in status:
                    log.info("UNSUCCESS, exiting", parent="DownloadManager")
                    break

                time.sleep(sleep_time)

            if status != "DONE":
                log.info(
                    f"UNSUCCESS, data tailor service took more that "
                    f"{DATA_TAILOR_TIMEOUT_LIMIT_MINUTES} minutes. "
                    f"The service may fail later on now",
                    parent="DownloadManager",
                )
            else:
                log.info("Customisation as been made", parent="DownloadManager")

            customisation = datatailor.get_customisation(customisation._id)
            (out,) = fnmatch.filter(customisation.outputs, "*")
            jobID = customisation._id
            log.info(
                f"Downloading outputs from Data Tailor job {jobID}. This can take ~2 minutes",
                parent="DownloadManager",
            )

            with customisation.stream_output(
                out,
            ) as stream, open(os.path.join(self.data_dir, stream.name), mode="wb") as fdst:
                filename = os.path.join(self.data_dir, stream.name)
                shutil.copyfileobj(stream, fdst)
                log.debug(f"Saved file to {filename}", parent="DownloadManager")

                # save to native file data store
                log.debug(
                    f"Copying file from {filename} to {data_store_filename_remote}",
                    parent="DownloadManager",
                )
                fs = fsspec.open(data_store_filename_remote).fs
                fs.put(filename, data_store_filename_remote)
                log.debug(
                    f"Copied file from {filename} to {data_store_filename_remote}",
                    parent="DownloadManager",
                )

            try:
                log.info(
                    f"Deleting job {jobID} from Data Tailor storage. This can take ~1 minute",
                    parent="DownloadManager",
                )
                customisation.delete()

            except Exception as e:
                log.warn(f"Failed deleting customization {jobID}: {e}", exc_info=True)


def get_filesize_megabytes(filename):
    """Returns filesize in megabytes"""
    filesize_bytes = os.path.getsize(filename)
    return filesize_bytes / 1e6


def eumetsat_filename_to_datetime(inner_tar_name):
    """Extracts datetime from EUMETSAT filename.

    Takes a file from the EUMETSAT API and returns
    the date and time part of the filename.

    Args:
        inner_tar_name: Filename part which contains the datetime information.

    Usage example:
        eumetsat_filename_to_datetime(filename)
    """

    p = re.compile(r"^MSG[1234]-SEVI-MSG15-0[01]00-NA-(\d*)\.")
    title_match = p.match(inner_tar_name)
    date_str = title_match.group(1)
    return datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")


def eumetsat_cloud_name_to_datetime(filename: str):
    """Takes a file from the EUMETSAT API and returns the it's datetime part for Cloud mask files"""
    date_str = filename.split("0100-0100-")[-1].split(".")[0]
    return datetime.datetime.strptime(date_str, "%Y%m%d%H%M%S")


class GOESDownloadManager:
    """
    Manager class for downloading GOES data.
    """
    def __init__(self, data_dir, log_directory=None):
        """
        Initialize the GOESDownloadManager.

        Args:
            data_dir (str): Directory to save downloaded GOES data.
            log_directory (str, optional): Directory to save logs.
            If None, logging is printed to STDOUT.
        """
        self.data_dir = data_dir
        self.ensure_directory_exists(self.data_dir)

        if log_directory:
            self.ensure_directory_exists(log_directory)
            logging.basicConfig(
                filename=os.path.join(log_directory, 'goes_download.log'),
                level=logging.INFO)
        else:
            logging.basicConfig(level=logging.INFO)

        logging.info(f"GOESDownloadManager initialized. Data will be saved to: {data_dir}")

    @staticmethod
    def ensure_directory_exists(directory):
        """Ensures the specified directory exists, creating it if necessary."""
        if not os.path.exists(directory):
            try:
                os.makedirs(directory)
                logging.info(f"Created directory: {directory}")
            except Exception as e:
                logging.error(f"Error creating directory {directory}: {e}")
                raise
    def download_goes_data(self, start_time, end_time, product='ABI-L1b-RadC',
                       domain='F', satellite=16):
        """
        Download GOES data for a specified time range and product.

        Args:
            start_time (datetime): Start of the download period.
            end_time (datetime): End of the download period.
            product (str): GOES product identifier. Default is 'ABI-L1b-RadC'.
            domain (str): Domain for the product. Default is 'F' (Full Disk).
            satellite (int): GOES satellite number. Default is 16.
        """
        G = GOES(satellite=satellite, product=product, domain=domain)
        current_time = start_time

        # Determine time increment based on product/domain
        time_increment = 1  # Default time increment (minutes)
        if product == 'ABI-L1b-RadC' and domain == 'F':
            time_increment = 10

        while current_time <= end_time:
            try:
                # Download the data
                ds = G.nearesttime(current_time)

                # Get acquisition time from the dataset
                acquisition_time = ds.time.data.item()

                # Format the acquisition time for filename
                date_string = acquisition_time.strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"goes_data_{date_string}.nc"
                filepath = os.path.join(self.data_dir, filename)

                # Check if data for current acquisition time already exists
                if os.path.exists(filepath):
                    logging.info(f"Data for {date_string} already exists. Skipping.")
                    current_time += datetime.timedelta(minutes=time_increment)
                    continue

                # Save to NetCDF
                ds.to_netcdf(filepath)
                print(ds)

                logging.info(f"Downloaded and saved GOES data to: {filename}")
            except Exception as e:
                logging.error(f"Error downloading GOES data for {current_time}: {e}")

            current_time += datetime.timedelta(minutes=time_increment)

        logging.info("Completed GOES data download.")

class DownloadManager:
    """
    Main download manager class to handle both EUMETSAT and GOES data downloading based on the provider.
    """
    def __init__(self, provider, user_key=None, user_secret=None, data_dir=None, log_directory=None):
        """
        Initialize the DownloadManager.

        Args:
            provider (str): Provider name ('EUMETSAT' or 'GOES').
            user_key (str): User key for accessing data (for EUMETSAT).
            user_secret (str): User secret for accessing data (for EUMETSAT).
            data_dir (str): Directory to save downloaded data.
            log_directory (str): Directory to save logs.
        """
        self.provider = provider

        if self.provider == "EUMETSAT":
            self.download_manager = EUMETSATDownloadManager(user_key, user_secret, data_dir, log_directory)
        elif self.provider == "GOES":
            self.download_manager = GOESDownloadManager(data_dir, log_directory)
        else:
            raise ValueError("Invalid provider. Supported providers are 'EUMETSAT' and 'GOES'.")

    def download_data(self, start_time, end_time):
        """
        Download data for the specified time range.

        Args:
            start_time (datetime): Start of the download period.
            end_time (datetime): End of the download period.
        """
        if self.provider == "GOES":
            self.download_manager.download_goes_data(start_time, end_time)


# Example usage
if __name__ == "__main__":
    provider = "GOES"
    user_key = "your_user_key"
    user_secret = "your_user_secret"
    data_dir = "/home/richa/Desktop/data"
    log_directory = "/home/richa/Desktop/log"

    start_time = datetime.datetime(2024, 3, 1, 0, 0)
    end_time = datetime.datetime(2024, 3, 1, 6, 0)

    if data_dir is not None:
        manager = DownloadManager(provider, None, None, data_dir, log_directory)
        manager.download_data(start_time, end_time)
    else:
        print("Error: 'data_dir' is not properly set.")
