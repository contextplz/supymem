// A failed connection has a finite retry budget. User actions never create a
// second timer or overlap a running request, and hidden windows do no work.
(function (scope) {
  function mount({ button, status, document, invoke, schedule = setTimeout, cancel = clearTimeout }) {
    const delays = [15000, 60000, 180000];
    let timer = null, cooldown = null, busy = false, stopped = false, attempt = 0;
    function clearTimer() { if (timer !== null) cancel(timer); timer = null; }
    function describe(result) {
      if (result.code === 'service_unavailable') return `The hosted service returned HTTP ${result.http_status || 'error'}.`;
      if (result.code === 'timeout') return 'The hosted service did not respond within eight seconds.';
      if (result.code === 'network_unavailable') return 'This Mac could not reach the hosted service.';
      return 'The hosted service did not return a usable application.';
    }
    function next(message) {
      clearTimer();
      if (stopped) return;
      const delay = delays[attempt];
      if (delay === undefined) {
        status.textContent = `${message} Automatic checks are paused after three attempts. Your saved sign-in is retained. Retry when service or network access is restored.`;
        return;
      }
      status.textContent = `${message} Next automatic check in ${delay / 1000} seconds (${attempt + 1} of 3).`;
      if (!document.hidden) timer = schedule(() => { timer = null; void retry(true); }, delay);
    }
    async function retry(automatic = false) {
      if (stopped || busy || (automatic && document.hidden)) return;
      if (!automatic && button.disabled) return;
      clearTimer();
      busy = true; button.disabled = true;
      if (automatic) attempt++;
      status.textContent = 'Checking the hosted connection…';
      try {
        const result = await invoke('desktop_retry_console');
        if (stopped) return;
        if (result?.connected === true) {
          stopped = true; status.textContent = 'Connected. Opening Supymem…';
        } else next(describe(result || {}));
      } catch {
        // Native IPC errors will not heal by repeatedly invoking the command.
        stopped = true;
        status.textContent = 'The native connection check failed. Automatic checks are paused. Quit and reopen Supymem to restore the connection controls.';
      } finally {
        busy = false;
        if (!stopped) cooldown = schedule(() => { cooldown = null; button.disabled = false; }, 10000);
      }
    }
    button.addEventListener('click', () => void retry(false));
    document.addEventListener('visibilitychange', () => {
      clearTimer();
      if (!document.hidden && !busy && !stopped && attempt < delays.length) next('Connection checks resumed.');
    });
    next('The hosted application is unavailable.');
    return { dispose() { stopped = true; clearTimer(); if (cooldown !== null) cancel(cooldown); } };
  }
  if (typeof module !== 'undefined') module.exports = { mount };
  else scope.SupymemConnection = { mount };
})(typeof window === 'undefined' ? globalThis : window);
