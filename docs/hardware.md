# Wiring, Calibration, and the GPU Map

Gamma 2 measures four GPUs, with a voltage tap and a current shunt for each
card. The shunts are on the 8-pin power cables, so these measurements exclude
the power supplied through the PCIe slot. Keep that limitation in mind when
comparing the results with `nvidia-smi`.

## Settings for the Current Wiring

The values below are recorded in `configs/default.yaml`. Leave them as they
are unless the wiring or calibration changes. The older single-GPU setup is
kept under `legacy/` for reference.

- One interleaved V/I channel pair per GPU on `Dev1`, `RSE`, −0.5…3.5 V:
  `ai0/ai1` (GPU1), `ai2/ai3` (GPU2), `ai4/ai5` (GPU3), `ai6/ai7` (GPU4)
- Per-GPU calibrated voltage divider ratios (12.18 V reference): `4.7411`,
  `4.6119`, `4.6119`, `4.6067`
- Shared 0.08 Ω shunt → current scale `12.5`. Each shunt sits in the GPU's
  ground return (`AI GND` on the supply side, `ai<n>` on the load side, so the
  input reads the shunt drop, not the 12 V rail). On gamma2 all four are
  wired the same way round (`current_sign: 1`); a shunt wired the other way
  reads negative under load and gets `current_sign: -1`.
- `nvidia-smi` index ↔ DAQ label on gamma2 (confirmed by loading one card at
  a time): index 0 = GPU1, 1 = GPU2, 2 = GPU3, 3 = GPU4, i.e. `gpu<N>` columns
  pair with `nvml/nvml.csv` index `N-1`. Re-check after any reseating or
  rewiring, since both orders can change. Under a full load each channel
  reads ~250-270 W against nvidia-smi's 280 W cap: the shunts are on the
  8-pin cables, so the ~30 W that comes through the PCIe slot is not
  measured.
- Sample rate `10000` Hz, power moving average `10` samples, CSV chunk
  rotation every `60` s
- CSV columns: `time_s`, then `gpuN_voltage_v / gpuN_current_a / gpuN_power_w`
  per GPU, then `total_power_w`

