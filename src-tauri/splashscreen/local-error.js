const parameters = new URLSearchParams(window.location.search);
const logPath = parameters.get('log');
document.getElementById('log-path').textContent = logPath || 'Log path unavailable. Check your app data folder, then retry.';
const status = document.getElementById('status');
const progress = document.getElementById('progress');
const retry = document.getElementById('retry');
const heading = document.getElementById('heading');
const invoke = window.__TAURI__?.core?.invoke || window.__TAURI_INTERNALS__?.invoke;
let timer;
let busy = false;
function fail(message) {
  document.getElementById('failure-help').hidden = false;
  heading.textContent = 'Your app needs another try';
  const [nextStep, ...details] = (message || 'Your app could not start. Select Retry to try again. Your saved work is retained.').split('\n');
  status.textContent = nextStep;
  const failureDetails = document.getElementById('failure-details');
  if (failureDetails) {
    failureDetails.textContent = details.join('\n');
    failureDetails.hidden = !failureDetails.textContent;
  }
  retry.hidden = false;
  progress.hidden = true;
}
async function checkProgress() {
  try {
    const roles = await invoke('backend_status');
    const labels = { core: 'your saved work', worker: 'your background tasks', console: 'your app screen' };
    progress.max = roles.length;
    progress.value = roles.filter(role => role.healthy).length;
    const waiting = roles.filter(role => !role.healthy).map(role => labels[role.role]).filter(Boolean);
    const maintenance = roles.find(role => role.maintenance_message)?.maintenance_message;
    const message = maintenance || (waiting.length ? `Getting ${waiting.join(' and ')} ready. You can keep this window open.` : 'Your app is ready. Opening your saved work.');
    if (status.textContent !== message) status.textContent = message;
    if (waiting.length) timer = setTimeout(checkProgress, 200);
  } catch {
    fail('Your app could not check startup progress. Select Retry to try again. If Retry does not help, quit and reopen your app.');
  }
}
retry.addEventListener('click', async () => {
  if (busy) return;
  busy = true;
  retry.disabled = true;
  clearTimeout(timer);
  heading.textContent = 'Charlie is getting your app ready';
  document.getElementById('failure-help').hidden = true;
  document.getElementById('failure-details').hidden = true;
  retry.hidden = true;
  progress.hidden = false;
  status.textContent = 'Trying again. You can keep this window open.';
  try {
    await invoke('desktop_retry_console');
  } catch {
    fail('Your app could not restart. Select Retry to try again, or check your startup log. Your saved work is retained.');
  } finally {
    busy = false;
    retry.disabled = false;
  }
});
if (parameters.get('starting') === '1') void checkProgress();
else fail(parameters.get('reason'));
