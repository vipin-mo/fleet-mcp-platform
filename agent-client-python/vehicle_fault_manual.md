# FLEET MANAGEMENT EXTRAORDINARY FAULT CODES MANUAL

## Error Code: ERR_BATT_THERMAL_RUNAWAY
- **Trigger Metrics:** Battery temperature stays well above the vehicle's own earlier baseline: a z-score above 2.5 sustained over the newest readings (classification `sustained_anomaly`), or a steady climb (`rising_trend`). A one-reading spike (`transient_spike`) is sensor noise and is not a trigger.
- **Root Cause:** Cellular degradation of internal cooling gel layers or localized cooling fan micro-failures.
- **Mitigation Directive:** The AI Orchestrator must deploy an Over-The-Air command setting the vehicle power mode parameter to 'eco_mode' to throttle core current draws and decrease thermodynamic dissipation rates.

## Error Code: ERR_CABIN_OVERHEATING_CRITICAL
- **Trigger Metrics:** Live cabin status register reporting 'overheating_critical' accompanied by sustained speed spikes.
- **Root Cause:** Climate control compressor power inverter component short circuit.
- **Mitigation Directive:** Isolate live tracking metrics, query tracking moving histories, and apply an active speed governor to mitigate cabin internal thermal pressure.

## Error Code: ERR_CABIN_COOLANT_LEAK
- **Trigger Metrics:** Live cabin status register reporting 'coolant_leak'. Speed and battery temperature can look normal.
- **Root Cause:** Refrigerant line leak in the climate control loop. The compressor runs dry and overheats under sustained road load.
- **Mitigation Directive:** Deploy an Over-The-Air command setting the speed governor to 30 km/h (parameter 'speed_governor', value '30'). A higher limit, such as the 50 km/h used for ERR_CABIN_OVERHEATING_CRITICAL, does not reduce compressor load enough and the fault stays active. Do not change the power mode: 'eco_mode' has no effect on this fault.
