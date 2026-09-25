For a bounded TCP service inventory:

1. Scan only the hosts and ports named in the task. Use TCP connect scanning
   (`-sT`) and numeric addresses (`-n`). If discovery probes are filtered, `-Pn`
   allows scanning the explicitly supplied hosts without assuming they are down.
2. Use `--host-timeout 45s` to keep each host bounded. A timeout or filtered port
   is not evidence that a service is absent. Report only observed open ports.
3. Normalize results into the requested JSON schema. Sort entries by host, then
   numeric port. Do not infer additional services or include explanatory prose.
