-- GPU core temperature (°C) per sample, so thermals can be tracked over long
-- unattended runs alongside power/utilization.
ALTER TABLE system_metrics ADD COLUMN gpu_temp_c REAL;
