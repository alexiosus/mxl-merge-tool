const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("ui.html", "utf8");
let script = html.split("<script>", 2)[1].split("</script>", 1)[0];
script = script.replace(
  "__MXL_MODEL__",
  JSON.stringify({
    paths: {},
    conflicts: [],
    previews: {
      semantic: {rows: [], total: 0, truncated: false, stats: {}},
      rendered: {provider: null, available: [], errors: {}, loading: false},
    },
  }),
);
script = script.replace("__MXL_TOKEN__", JSON.stringify("token"));
new vm.Script(script, {filename: "ui.html"});
