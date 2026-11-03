# deploy/

The systemd units are no longer hand-edited templates: they are generated and installed by the
CLI, so the paths are always right and there is nothing to `sed`.

```bash
uv run coffee-kb collector install   # ~/.config/systemd/user/coffee-kb-collector.{service,timer}
uv run coffee-kb web install         # ~/.config/systemd/user/coffee-kb-web.service
sudo loginctl enable-linger "$USER"  # once, so user units run without a login session
```

`uninstall` removes them again. The unit contents live in `src/coffee_kb/service.py`
(`render_collector_service`, `render_collector_timer`, `render_web_service`); see the README's
"Home server" section for the full setup.
