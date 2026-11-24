# SpoolPrint

[![vibe-coded](https://img.shields.io/badge/vibe--coded-Claude%20Code-D97757)](https://claude.ai/code)

_A small helper app to batch print 3D printer filament labels to a Niimbot label printer._

SpoolPrint lists the spools in your [3D Filament Profiles](https://3dfilamentprofiles.com) account and prints
their labels to a Niimbot printer over Bluetooth, straight from the browser. Print one label, or tick a batch
and print them in one go. It can also print a label for a filament, with a QR code that opens the filament's
page, on its own or followed by its spools' labels.

**Unofficial.** SpoolPrint isn't affiliated with or endorsed by 3D Filament Profiles or Niimbot.

## Features

- A sortable table of your spools: newest purchase first, or by colour, name, material, location and more.
  Group by filament or material, search, filter by location and hide spent spools.
- Print spool labels and filament labels singly or in batches, with a progress bar and cancel.
- Choose what each label shows: four properties and a colour area, separately for spool and filament labels.
- Print settings per printer: density, rotation and an offset to nudge the print, plus a calibration label
  that shows the label's edges.
- Labels are cached, so each one costs one API request the first time and none after that. The page shows how
  many requests a batch will use and how many you have left this month.

## Requirements

- A Niimbot printer. SpoolPrint is built and tested on the **B1** with **40 × 30 mm** labels. It uses
  [niimbluelib](https://github.com/MultiMote/niimbluelib), so other models it supports may work, but they
  haven't been tested.
- A [3D Filament Profiles](https://3dfilamentprofiles.com) account with an API key.
- Docker with Compose.
- Chrome or Edge, on a computer or Android, to print. Printing uses Web Bluetooth, which Firefox and Safari
  (including every browser on iPhone and iPad) don't support.

## Deploy

```bash
curl -O https://raw.githubusercontent.com/SiDtheTurtle/spoolprint/main/compose.yml
curl -o .env https://raw.githubusercontent.com/SiDtheTurtle/spoolprint/main/.env.example
# edit .env and paste in your API key
docker compose up -d
```

The page is then at `http://<host>:6464`.

**Printing needs HTTPS.** Browsers only allow Bluetooth on secure pages: `https://` or `http://localhost`.
Put SpoolPrint behind a reverse proxy with a certificate (Caddy, Traefik, nginx and so on), or open it on
the machine it runs on. Over plain `http://` from another machine, you can browse spools but not print.

The page has no login. Keep it on your own network, not on the internet. Your API key stays on the server and
is never sent to the browser.

## Use

1. **Connect printer.** Turn the printer on and pick it from the browser's list.
2. **Settings.** Choose what the labels show. If the print isn't centred, print the calibration label and
   adjust the offset. Printer settings are saved for each printer, by its Bluetooth name.
3. **Preview** a spool to see its label exactly as it will print, then **Print this label**.
4. **Batches.** Tick spools, or whole groups, then **Print labels**. To include filament labels, group by
   filament and tick **Filament label** on a group. Each filament's label prints before its spools.

**Refresh list** fetches your spools again after you change them on 3D Filament Profiles.

## Back up

Everything is in the `spoolprint_data` volume:

| What | Back up? | If it's lost |
|---|---|---|
| `settings.json` | **Yes** | You set up label content and each printer's settings again |
| `labels/` | Optional | Each label is downloaded again the first time it's printed, at one API request each |
| `spools.json`, `quota.json` | No | Fetched again on the next refresh |
| `.env` | **Yes**. **Secret** | You copy your API key from 3D Filament Profiles again |

SpoolPrint writes each file in one step, so it's safe to copy while it's running:

```bash
docker run --rm -v spoolprint_data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/spoolprint-$(date +%F).tar.gz -C /data .
```

To restore, stop SpoolPrint and extract the archive into the volume the same way, then start it again.

## AI disclosure

I have a developer background, but this was ripe for AI-assisted coding. SpoolPrint was built entirely
through pair-programming with Claude Code. I made the design and architectural decisions, and steered the AI
to make sure there were no obvious security flaws and the UI was logical for humans. Claude Code did the
typing.

## Credits

- [niimbluelib](https://github.com/MultiMote/niimbluelib) (MIT) talks to the printer. A copy is included in
  `static/vendor`.
- Labels are made by [3D Filament Profiles](https://3dfilamentprofiles.com) through its API.
- [segno](https://github.com/heuer/segno) draws the QR codes on filament labels.

## Licence

[GPL-3.0](LICENSE).
