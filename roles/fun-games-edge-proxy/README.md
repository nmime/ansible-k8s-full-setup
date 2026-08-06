# fun-games-edge-proxy

Manages the Fun-Games edge proxy: Nginx/OCSP configuration, certificate
staging, and cutover control for the fun-games edge nodes.

## Key variables

- `fun_games_edge_mode` — `audit` or `enforce`
- `fun_games_edge_confirm_cutover` — must be `true` to apply traffic changes
- `fun_games_edge_install_root` — on-node install directory
- `fun_games_edge_backup_root` — on-node backup directory

## Where applied

Invoked by `playbooks/fun-games-edge.yml` and related fun-games edge playbooks.
