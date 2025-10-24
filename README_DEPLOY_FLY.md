# Deploy a Flask app to Fly.io (Freshs_labors)

## Prereqs
- Install Fly CLI: https://fly.io/docs/hands-on/install-flyctl/
- Create an account: `fly auth signup` or `fly auth login`

## One-time setup in your project folder
```bash
# inside the project root
fly launch --no-deploy
# When prompted:
#   - App Name: choose a UNIQUE name (e.g., freshs-labors-bayron-123)
#   - Select region: "gru" (São Paulo) or "mia" (Miami) works well from Colombia.
#   - Use a Postgres DB? -> No (we'll use SQLite on a volume)
#   - Would you like to deploy now? -> No
```

This will create a `fly.toml` if not present. If you already have one from this repo,
**edit the `app =` line** to your unique name.

## Create a persistent volume
```bash
fly volumes create data --size 1 --region gru
# If you chose Miami then use --region mia
```

## Set secrets
```bash
# IMPORTANT: use a strong secret; don't reuse your local .env value
fly secrets set SECRET_KEY="cambia-esto-por-uno-muy-seguro"
# (Optional, only if you'll use real WhatsApp OTP via Twilio)
fly secrets set TWILIO_SID="..." TWILIO_TOKEN="..." TWILIO_FROM="whatsapp:+1..."
```

## Deploy
```bash
fly deploy
```

The app will build the Docker image and boot with Gunicorn on port 8080.

## Verify
```bash
fly status
fly logs
fly open          # opens https://<your-app-name>.fly.dev
```

## Notes about SQLite & uploads
- This template mounts a volume at `/data` and points `DATABASE_URL` to `sqlite:////data/database.db`.
- Your app also writes to `uploads/`. The Dockerfile sets `UPLOAD_FOLDER=/data/uploads` so uploads persist.
- On first boot, your app's own migration function `migrate_sqlite()` will initialize tables if needed.

## Changing regions or autoscaling
- Edit `primary_region` in fly.toml (`gru`, `mia`, etc.).
- For test traffic from varios países / VPNs, you can add more regions and scale to 1 machine in each.
  Keep in mind this can duplicate instances (stateless recommended). For SQLite + single-writer, **stick to 1 region**.

## If you get a 502/503
- Check logs: `fly logs`
- Ensure `fly secrets list` shows `SECRET_KEY`
- Confirm there's **no** hard-coded Windows path in your env (`DATABASE_URI` in `.env` must NOT be used here).
  We rely on `DATABASE_URL` from `fly.toml`.
- If static uploads 404: confirm `/data/uploads` exists (it will be created on demand by the app).

## Redeploy after local changes
```bash
fly deploy
```
