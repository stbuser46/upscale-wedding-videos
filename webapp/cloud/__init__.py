"""Cloud (RunPod) execution backend for durable-unit restoration.

The control plane stays on the homeserver: the catalog, the job state machine,
`job_chunks` durability, unit validation, lossless assembly and artifact
registration are all unchanged. Only stage 3 — one ~750-frame SeedVR2 unit —
is dispatched to a rented GPU.
"""
