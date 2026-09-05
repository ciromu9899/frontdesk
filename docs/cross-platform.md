# FrontDesk cross-platform edition

FrontDesk 1.6 runs as a browser application. The same release works on Windows,
macOS and Linux hosts; operators and customers use a modern browser on a PC,
tablet, iPhone or Android device. No native desktop UI is required.

## What is included

- customer web chat on port `8766`;
- shared inbox and administration on port `8765`;
- a persistent named volume for conversations, knowledge, handoffs and webhook
  deduplication;
- an Echo demonstration with no model download;
- an optional Ollama composition that downloads and runs the local model inside
  containers.

Docker Desktop, Docker Engine with Compose, or a compatible OCI/Compose runtime
is required on the host. Customers do not install Python or Ollama directly.

## First start without a model download

1. Copy `compose.env.example` to `.env`.
2. Generate two independent secrets and paste them into the matching empty
   fields. With Python installed, run `python auth.py --new-secret` twice. With
   Docker only, build first and run this command twice:

   ```console
   docker compose build
   docker compose run --rm --no-deps frontdesk python auth.py --new-secret
   ```

3. Start the product:

   ```console
   docker compose up -d
   ```

4. Issue an administrator token without exposing the signing secret:

   ```console
   docker compose exec frontdesk python auth.py --subject owner@example.com --roles admin --tenant salon:default --hours 24
   ```

5. Open `http://127.0.0.1:8766/` for customer chat and
   `http://127.0.0.1:8765/login` for the shared inbox.

The default answer engine is Echo so the installation can be verified without
downloading a model or sending data to a provider.

## Start with containerized Ollama

After the secrets are configured, run:

```console
docker compose -f compose.yaml -f compose.ollama.yaml up -d --build
```

The first launch downloads the selected model into the `ollama-data` volume.
Later application upgrades do not download it again. Set `OLLAMA_MODEL` in
`.env` to choose another installed Ollama model.

## Use an Ollama server already on the host or network

Set these values in `.env`, then use the normal `docker compose up -d` command:

```dotenv
FRONTDESK_WEB_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

On Linux, the supplied `host-gateway` mapping gives the container the same
hostname. For a different trusted machine, use its private HTTP address or its
HTTPS endpoint.

## Phone, LAN and public deployment

The safe default publishes both ports only to the current computer. For access
from another device, put FrontDesk behind a trusted HTTPS reverse proxy and set:

```dotenv
FRONTDESK_BIND_ADDRESS=0.0.0.0
FRONTDESK_ADMIN_BIND_ADDRESS=0.0.0.0
FRONTDESK_SECURE_COOKIES=1
FRONTDESK_EMBED_ORIGINS='self' https://www.example.com
```

Do not expose either HTTP port directly to the internet. Terminate TLS at the
reverse proxy, restrict the administration hostname, and keep the administrator
token private. Connector webhooks should use the public HTTPS origin.

## Direct Python launch

The same headless server also runs without containers on Windows, macOS and
Linux when Python 3.11 or newer is already installed:

```console
python server.py --host 127.0.0.1
```

`FRONTDESK_AUTH_SECRET` must contain at least 32 characters. Data is stored in
`data/` unless `FRONTDESK_DATA_DIR` selects another durable directory.

## Stop and update

```console
docker compose down
docker compose build --pull
docker compose up -d
```

`docker compose down` preserves the named data volumes. Deleting either named
volume deletes customer records and model files and is not part of an update.
