const path = require("node:path");
const parent = Number(process.env.SUPYMEM_PARENT_PID);
if (!Number.isSafeInteger(parent) || parent <= 0 || !process.argv[2]) {
  throw new Error("Your app could not verify its startup. Quit and reopen your app to retry.");
}
setInterval(() => {
  try { process.kill(parent, 0); }
  catch (error) { if (error.code === "ESRCH") process.exit(0); }
}, 2000).unref();
require(path.resolve(process.argv[2]));
