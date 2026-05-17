# soundWave

soundWave is an NVDA add-on that renders text to an audio file using installed speech engines.

## Quick Start

1. Press `NVDA+Ctrl+=` to open soundWave.
2. Choose a synthesizer.
3. Choose the input source: clipboard, text file, or typed/pasted text.
4. Configure the synthesizer and use `Test` to preview settings.
5. Choose an output file and render.

## Features

- Render clipboard text, text files, or typed text.
- Export WAV files, with MP3 export available when ffmpeg is installed.
- Render large inputs in chunks.
- Use synthesizer-specific settings where available.
- Check GitHub releases for updates.

## Documentation

- Full help: [`source/doc/en/readme.html`](source/doc/en/readme.html)

## Source Code

- Extracted source for this build: [`source/`](source/)
- Main plugin: [`source/globalPlugins/soundWave.py`](source/globalPlugins/soundWave.py)

## Install

1. Download `soundWave.nvda-addon` from Releases.
2. In NVDA, open Add-on Manager and choose Install.
3. Select the file and restart NVDA when prompted.

Latest packaged add-on: [`soundWave.nvda-addon`](./soundWave.nvda-addon)
