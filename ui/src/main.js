const { invoke } = window.__TAURI__.core;

let logOutputEl;

async function runScript(scriptName) {
  logOutputEl.textContent = `Running ${scriptName}...\n`;
  try {
    const output = await invoke("run_script", { scriptName, args: [] });
    logOutputEl.textContent += `\nSuccess:\n${output}`;
  } catch (err) {
    logOutputEl.textContent += `\nError:\n${err}`;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  logOutputEl = document.querySelector("#log-output");

  document.querySelector("#btn-audit").addEventListener("click", () => {
    runScript("Audit-DjLibraryCleanup.ps1");
  });

  document.querySelector("#btn-add").addEventListener("click", () => {
    runScript("Add-RekordboxProcessedContent.ps1");
  });
});
