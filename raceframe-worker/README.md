# RaceFrame worker

The worker performs OCR and face processing without PostgreSQL or R2
credentials. It claims leased jobs through token-protected backend endpoints,
downloads only signed objects, and reports results with attempt fencing.

Production uses a reviewed immutable image digest. Prepare non-root mounts once:

```bash
sudo install -d -m 0750 -o 10001 -g 10001 model-cache
sudo install -d -m 0750 -o root -g 10001 secrets
sudo install -m 0640 -o root -g 10001 /secure/source/google-vision.json \
  secrets/google-vision.json
cp .env.example .env
chmod 0600 .env
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose logs --tail 100 worker
```

Set `WORKER_VERSION` to the release commit and use a unique stable `WORKER_ID`
per process. The chosen InsightFace model must have explicit commercial-use
approval before deployment. Stop/drain an old worker before upgrading the
backend protocol, then deploy the matching worker image after the migration.

See the [production runbook](../docs/operations/production-runbook.md) and
[configuration reference](../docs/operations/configuration.md).
