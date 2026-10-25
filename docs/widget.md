# Phone widget

`/api/widget` returns a few lines of read-only data (battery %, solar, home and grid power, today's saving). A free [Scriptable](https://scriptable.app) script turns it into an iPhone home-screen or lock-screen widget. iOS refreshes widgets every 5 to 15 minutes, so it is not live to the second.

## 1. Turn it on

Set `WIDGET_TOKEN` on the stack to a long random string (16 characters or more), then redeploy. Without it the endpoint answers 404. The token is sent in an `Authorization: Bearer` header, never in the URL.

```
curl -H "Authorization: Bearer $WIDGET_TOKEN" http://<pi-ip>:<port>/api/widget
```

## 2. Reach it from outside the house

Pick one. The token only unlocks `/api/widget`, but the rest of the dashboard reads are open, so don't publish all of it.

**Tailscale (simplest).** Install Tailscale on the Pi and the phone; use the Pi's Tailscale address in the script. Nothing is exposed to the internet.

**Cloudflare Tunnel, widget path only.**
1. Cloudflare Zero Trust, Networks, Tunnels: create a tunnel and copy its token.
2. Add a public hostname such as `anker.<your domain>` pointing to `http://<pi-ip>:<dashboard port>`.
3. Run `cloudflare/cloudflared` as a container on the Pi (in Orchestrator: an image project) with the environment variable `TUNNEL_TOKEN` and the command `tunnel run`.
4. In Cloudflare, add a WAF custom rule on that hostname: block when the path is not `/api/widget`. That keeps the dashboard and battery control private.
5. Optional: put Cloudflare Access in front with a service token, so only your phone gets through.
6. Set `TRUST_PROXY=true` on the stack so failed-token limits count per visitor.

## 3. Add the widget

Install Scriptable, create a script from `docs/widget-scriptable.js`, set `URL` (for example `https://anker.<your domain>/api/widget`) and `TOKEN`, then add a Scriptable widget to the home screen and choose the script.
