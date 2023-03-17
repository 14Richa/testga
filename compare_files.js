// const fs = require("fs");

// const file1 = "config/TSC_MEMBER.json";
// const file2 = "parentRepo1/config/TSC_MEMBER.json";

// const content1 = fs.readFileSync(file1, "utf8");
// const content2 = fs.readFileSync(file2, "utf8");

// if (content1 !== content2) {
//   console.log("::set-output name=diff::true");
// } else {
//   console.log("::set-output name=diff::false");
// }

const oldList = "parentRepo1/config/TSC_MEMBER.json";
const newList = "config/TSC_MEMBER.json";

const added = newList.filter(
  (newObj) => !oldList.some((oldObj) => oldObj.name === newObj.name)
);
const removed = oldList.filter(
  (oldObj) => !newList.some((newObj) => newObj.name === oldObj.name)
);

console.log("Added objects:", added);
console.log("Removed objects:", removed);
