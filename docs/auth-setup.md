# ArrHub — Authentication & HTTPS Setup Guide

This guide explains how to configure authentication and run ArrHub behind HTTPS.

---

## 1. Default Behavior

When the WebUI container starts for the first time with no `ARRHUB_TOKEN` set:

1. A random 32-character token is automatically generated
2. It is printed to container logs:
   ```bash
   docker logs arrhub_webui | grep "ARRHUB TOKEN"
   ```
3. The WebUI shows a login screen — enter the token to access
4. The token is stored (hashed) in SQLite at `/data/arrhub.db`

---

## 2. Setting a Custom Token

**Option 1: Environment variable (recommended)**
```bash
docker run -d \
  -p 9999:9999 \
  -e ARRHUB_TOKEN="your-secret-token-here" \
  -v /opt/arrhub/data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --name arrhub_webui \
  ghcr.io/twoeagles404/arrhub:latest
```

**Option 2: In docker-compose.yml**
```yaml
services:
  arrhub_webui:
    image: ghcr.io/twoeagles404/arrhub:latest
    environment:
      - ARRHUB_TOKEN=your-secret-token-here
    volumes:
      - /opt/arrhub/data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    ports:
      - "9999:9999"
```

**Option 3: After install, edit the .env file**
```bash
echo "ARRHUB_TOKEN=your-secret-token-here" >> /opt/arrhub/data/.env
docker restart arrhub_webui
```

---

## 3. Disabling Auth (LAN-only, trusted network)

```bash
docker run -d \
  -p 9999:9999 \
  -e ARRHUB_NO_AUTH=true \
  -v /opt/arrhub/data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --name arrhub_webui \
  ghcr.io/twoeagles404/arrhub:latest
```

> ⚠️ **Warning:** Only use this if the port is NOT exposed to the internet.

---

## 4. Putting ArrHub Behind HTTPS

For production use, always put ArrHub behind a reverse proxy with HTTPS.

### 4A. Nginx

```nginx
server {
    listen 80;
    server_name arrhub.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name arrhub.yourdomain.com;

    ssl_certificate     /etc/ssl/certs/your.crt;
    ssl_certificate_key /etc/ssl/private/your.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location / {
        proxy_pass         http://localhost:9999;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # ── Required for SSE (live dashboard updates) ──
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600;

        # ── Required for WebSocket (log streaming) ──
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
    }
}
```

### 4B. Caddy

```caddyfile
arrhub.yourdomain.com {
    reverse_proxy localhost:9999 {
        flush_interval -1
    }
}
```

Caddy handles HTTPS automatically via Let's Encrypt. The `flush_interval -1` directive ensures SSE works correctly.

### 4C. Nginx Proxy Manager (NPM)

1. Add a new Proxy Host pointing to `localhost:9999`
2. Enable SSL with Let's Encrypt
3. In **Advanced** tab, add:
   ```nginx
   proxy_buffering off;
   proxy_cache off;
   proxy_read_timeout 3600;
   ```

---

## 5. Token Security Notes

- The token is stored as an Argon2-hashed value in SQLite — it cannot be recovered if lost
- If you forget your token, delete `/opt/arrhub/data/arrhub.db` and restart — a new token will be generated
- Tokens are transmitted as `Authorization: Bearer <token>` headers over HTTPS
- On LAN-only setups, HTTP is acceptable; for internet-exposed deployments, HTTPS is mandatory

---

## 6. Changing Your Token

To change your token:

```bash
# Set new token via environment variable and restart
docker stop arrhub_webui
docker run -d \
  -e ARRHUB_TOKEN="new-token-here" \
  ... (other flags) ...
  --name arrhub_webui \
  ghcr.io/twoeagles404/arrhub:latest
```

Or via Settings tab in the WebUI (if you know the current token).

---

*ArrHub — MIT Licensed · https://github.com/twoeagles404/arrhub*
