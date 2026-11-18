// Polling: fetch the snapshot on an interval and update state. The server is
// the source of truth; the UI just re-renders from each snapshot.
import { api } from './api.js';
import { state } from './state.js';

let timer = null;

// Start polling. `options.skipFirst` avoids an immediate fetch when the caller
// (boot) already loaded a snapshot — it schedules the first tick one interval
// out instead, using the interval from the snapshot already in state.
export function startPolling(onError, { skipFirst = false } = {}) {
  const schedule = (intervalS) => {
    if (timer) clearInterval(timer);
    timer = setInterval(tick, Math.max(3, intervalS || 15) * 1000);
  };
  const tick = async () => {
    try {
      const snap = await api.snapshot();
      state.setSnapshot(snap);
      // Reschedule with the server-provided interval.
      schedule(snap.poll_interval_s);
    } catch (e) {
      if (onError) onError(e);
    }
  };
  if (skipFirst) {
    schedule(state.snapshot?.poll_interval_s);
  } else {
    tick();
  }
}

export async function refreshNow() {
  state.setSnapshot(await api.snapshot());
}
