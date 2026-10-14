'use strict';
// Session-only view state. No tokens, policies, or telemetry in browser storage.
window.CapacityPanel = (() => {
  const get = id => document.getElementById(id);
  let generation = 0;
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const rate = value => number(value) ? `${(value / 1e6).toFixed(1)} MB/s` : '—';
  const volume = value => number(value) ? `${(value / 1e9).toFixed(2)} GB` : '—';
  const reasons = {
    route_unvalidated: 'The migration route has not been validated.',
    telemetry_unavailable: 'Fresh, attributed link measurements are unavailable.',
    no_safe_reservation: 'No fixed bandwidth reservation is configured.',
    static_reservation: 'Using the configured fixed bandwidth reservation.',
    runtime_limit: 'The current run limits the available worker count.',
    quota_limit: 'The worker quota limits capacity.',
    worker_cost_limit: 'The configured worker budget limits capacity.',
    capacity_constrained: 'Available capacity is below the calculated demand.',
    provisioning_inputs_unknown: 'Quota or cost inputs are not configured.',
    calibration_required: 'A measured worker throughput profile is needed.',
    scale_up_stabilizing: 'Waiting for sustained demand and worker stabilization.',
    scale_down_stabilizing: 'Waiting before recommending that excess workers drain.',
    telemetry_recovery: 'Waiting for three fresh measurement windows.',
    congestion_backoff: 'Congestion detected; reduce migration bandwidth.',
    pending_capacity: 'Waiting for requested workers to become ready.',
    physical_capacity_reserved: 'Existing or retiring nodes occupy the capacity limit.',
    zero_bandwidth_budget: 'No migration bandwidth is currently available.',
    queue_tail: 'Too little work remains to benefit from another worker.',
    queue_empty: 'No runnable work remains.',
    recommend_scale_up: 'Additional workers could improve throughput.',
    recommend_drain: 'Excess workers can drain after their current work finishes.',
    operator_hold: 'The operator has held capacity.',
    run_limit_reached: 'The run has reached its scheduling allowance or deadline.',
    unbound: 'Start the reviewed run before evaluating active capacity.',
    complete: 'The migration is complete.',
    cancelled: 'The run is cancelled.', paused: 'New work is paused.',
    draining: 'Active work is draining.', hold: 'Keep the current capacity.'
  };
  const explain = key => reasons[key] || key.replaceAll('_', ' ');
  function clear(message = 'Select a migration to see its measurements.') {
    generation++;
    for (const id of ['pipe-usage','source-rate','verified-rate','recommended-workers',
                      'source-total','copy-total','verify-total','retry-total','network-rx','network-tx']) get(id).textContent = '—';
    get('pipe-meter').value = 0;
    get('network-status').textContent = message;
    get('capacity-reason').textContent = '';
    get('measurement-coverage').textContent = '';
    get('capacity-history').replaceChildren();
    get('network-workers').replaceChildren();
    get('worker-network-status').textContent = '';
    get('worker-measurements').replaceChildren();
  }
  function render(data) {
    const net = data.network, metrics = data.worker_metrics, decision = data.recommendation;
    get('pipe-usage').textContent = number(net.utilization_fraction) ? `${(net.utilization_fraction * 100).toFixed(1)}%` : '—';
    get('pipe-meter').value = number(net.utilization_fraction) ? Math.min(1, net.utilization_fraction) : 0;
    const status = {not_configured:'Network monitoring not connected',unavailable:'Network measurement unavailable',
      fresh:'Fresh attributed link data',slow_unattributed:'Aggregated link data; migration share unknown',
      worker_network:'OCI worker network measurements connected; pipe utilization unknown',
      partial:'Some OCI worker network measurements are missing',
      stale:'Network measurement is stale'}[net.status] || 'Link measurement could not be validated';
    const age = number(net.age_seconds) ? ` · ${Math.round(net.age_seconds)} seconds old` : '';
    const reserve = number(net.headroom_fraction) ? ` · ${(net.headroom_fraction*100).toFixed(0)}% reserved` : '';
    get('network-status').textContent = status + age + reserve;
    const workerNet = net.scope === 'worker_vnics' ? net : net.worker_network;
    const traffic = value => number(value) ? value < 1e6 ? `${(value/1e3).toFixed(1)} kB/s` : rate(value) : '\u2014';
    get('network-rx').textContent = traffic(workerNet?.receive_bytes_per_second);
    get('network-tx').textContent = traffic(workerNet?.send_bytes_per_second);
    get('worker-network-status').textContent = workerNet
      ? `${workerNet.measured_vnics} of ${workerNet.expected_vnics} interfaces reporting - One-minute averages of all worker traffic, including verification and database activity. These rates do not measure shared-pipe utilization.`
      : 'Worker network monitoring not connected.';
    get('network-workers').replaceChildren(...(workerNet?.workers || []).map(worker => {
      const item = document.createElement('p');
      item.textContent = `${worker.node_id} - ${worker.status} - Received ${traffic(worker.receive_bytes_per_second)} - Sent ${traffic(worker.send_bytes_per_second)}`;
      return item;
    }));
    get('source-rate').textContent = rate(metrics.active_source_bytes_per_second);
    get('verified-rate').textContent = rate(metrics.verified_average_bytes_per_second);
    const blocked = decision && decision.reasons.some(r => ['route_unvalidated','telemetry_unavailable','calibration_required'].includes(r));
    get('recommended-workers').textContent = decision && !blocked ? String(decision.desired_nodes) : 'Awaiting data';
    get('capacity-reason').textContent = decision ? decision.reasons.map(explain).join(' ') : data.reason;
    const measured = metrics.measured_attempts > 0;
    get('source-total').textContent = volume(measured ? metrics.source_read_bytes : null);
    get('copy-total').textContent = volume(measured ? metrics.totals.copy_bytes : null);
    get('verify-total').textContent = volume(measured ? metrics.totals.destination_verify_bytes : null);
    get('retry-total').textContent = volume(measured ? metrics.totals.source_retry_bytes : null);
    get('measurement-coverage').textContent = metrics.coverage === 'unavailable'
      ? 'Worker telemetry is unavailable for this run. Earlier runs cannot be reconstructed from copy totals.'
      : `${metrics.measured_attempts} measured attempts · ${metrics.coverage === 'partial' ? 'Partial coverage; displayed totals are lower bounds.' : 'Application-byte counters recorded.'} Retry bytes are included in source reads. Verified speed is the average since the first claim.`;
    get('worker-measurements').replaceChildren(...metrics.workers.map(worker => {
      const item = document.createElement('p');
      item.textContent = `${worker.worker_id} · ${worker.phase.replaceAll('_',' ')} · ${worker.status} · ${rate(worker.source_bytes_per_second)}`;
      return item;
    }));
    get('capacity-history').replaceChildren(...data.history.slice(-10).reverse().map(item => {
      const row = document.createElement('tr');
      for (const value of [new Date(item.at_seconds*1000).toLocaleTimeString(),item.desired_nodes,
                           rate(item.budget_bytes_per_second),item.reasons.map(explain).join(' ')]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      return row;
    }));
  }
  async function refresh(api, job) {
    const request = ++generation;
    try {
      const value = await api(`/api/jobs/${job}/capacity`);
      if (request === generation) render(value);
    } catch (_) {
      if (request === generation) clear('Capacity measurements are unavailable. Refresh to retry.');
    }
  }
  return {refresh,clear};
})();
