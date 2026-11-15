// Polling: fetch the snapshot on an interval and update state. The server is
// the source of truth; the UI just re-renders from each snapshot.
import { api } from './api.js';
import { state } from './state.js';

let timer = null;

export function startPolling(onError) {
  const tick = async () => {
    try {
      const snap = await api.snapshot();
      state.setSnapshot(snap);
      // Reschedule with the server-provided interval.
      if (timer) clearInterval(timer);
      timer = setInterval(tick, Math.max(3, snap.poll_interval_s || 15) * 1000);
    } catch (e) {
      if (onError) onError(e);
    }
  };
  tick();
}

export async function refreshNow() {
  state.setSnapshot(await api.snapshot());
}
