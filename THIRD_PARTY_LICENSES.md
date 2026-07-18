# Third-party licenses

This app's own code (the `seestar_bridge` and the add-on packaging) is licensed
under the [Apache License, Version 2.0](LICENSE).

## seestar_alp (GPL-3.0)

The built image bundles the [seestar_alp](https://github.com/smart-underworld/seestar_alp)
Alpaca driver, pinned to upstream tag **`v3.2.2`** (see
[`seestar/Dockerfile`](seestar/Dockerfile), `SEESTAR_ALP_REF`). seestar_alp is
licensed under the **GNU General Public License, version 3.0** and is bundled at
arm's length — it runs as a separate process that this app talks to only over the
Alpaca HTTP socket.

The complete, unmodified license texts are cloned from the pinned tag and
preserved inside the image at `/app/seestar_alp/`:

| File | Component | License |
|---|---|---|
| `LICENSE.txt` | GPL-3.0 license text | GPL-3.0 |
| `LICENSE-Seestar_Alp.txt` | seestar_alp — © 2024 Kai Yung / smart-underworld | GPL-3.0 |
| `LICENSE-AlpacaDevice.txt` | AlpacaDevice — © Bob Denny / ASCOM Initiative | MIT |
| `LICENSE-PyIndi.txt` | PyIndi | BSD-3-Clause |

To read them from a running container:

```
docker exec <container> cat /app/seestar_alp/LICENSE.txt
```

The corresponding source is the public upstream repository at the pinned tag:
<https://github.com/smart-underworld/seestar_alp/tree/v3.2.2>.

### What this means if you redistribute the image

The add-on's own code stays Apache-2.0. But because the **distributed GHCR image
includes GPL-3.0 software** (seestar_alp), anyone who redistributes the image is
subject to the **GPL-3.0** for that component — in practice: keep the bundled
license texts intact and make the corresponding source available (it already is,
at the pinned-tag URL above). We take this conservative stance regardless of the
arm's-length process boundary, so the GPL obligation is unambiguous. If you build
your own image from a different `SEESTAR_ALP_REF`, update the corresponding-source
reference to match.

See [NOTICE](NOTICE) for the attribution summary.
