// const fs = require("fs");

// const file1 = "config/TSC_MEMBER.json";
// const file2 = "parentRepo1/config/TSC_MEMBER.json";

// const content1 = fs.readFileSync(file1, "utf8");
// const content2 = fs.readFileSync(file2, "utf8");

// if (content1 !== content2) {
//   console.log("::set-output name=diff::true");
// } else {
//   console.log("::set-output name=diff::false
// }

const fs = require("fs");

// Read the contents of the two JSON files
const file2 = JSON.parse(fs.readFileSync("parentRepo1/config/TSC_MEMBER.json"));
const file1 = JSON.parse(fs.readFileSync("config/TSC_MEMBER.json"));
console.log("FILE1", file1);
console.log("FILE2", file2);

// Compare the two JSON files via the "name" field
// const differences = file1.filter((obj1) => {
//   const obj2 = file2.find((obj2) => obj1.name === obj2.name);
//   if (obj2) {
//     return JSON.stringify(obj1) !== JSON.stringify(obj2);
//   }
//   return true;
// });

// // If the differences array is not empty, print the differences
// if (differences.length) {
//   console.log("The following differences:");
//   console.log(differences);
// } else {
//   console.log("The files are identical.");
// }
const newList = file1;
const oldList = file2;

const added = newList.filter(
  (newObj) => !oldList.some((oldObj) => oldObj.name === newObj.name)
);
const removed = oldList.filter(
  (oldObj) => !newList.some((newObj) => newObj.name === oldObj.name)
);

console.log("Added objects:", added);
console.log("Removed objects:", removed);
