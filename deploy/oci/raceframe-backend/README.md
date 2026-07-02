# RaceFrame backend service

This folder is intended to live on the OCI VPS at:

`~/server-services/raceframe-backend`

It runs the minimal FastAPI backend for the RaceFrame MVP.

Current local routing:

- container port: `8000`
- host bind: `127.0.0.1:8008`

That host bind is designed for a reverse proxy or Cloudflare Tunnel origin target.
