# soundWave

soundWave is an NVDA add-on that renders text to an audio file using installed speech engines.

## Quick Start

1. Press `NVDA+Ctrl+=` to open soundWave.
2. Choose a synthesizer.
3. Choose the input source: clipboard, text file, folder of text files, or typed/pasted text.
4. Configure the synthesizer and use `Test` to preview settings.
5. Choose an output file and render.

## Features

- Render clipboard text, text files, folders of text files, or typed text.
- Export WAV files, with MP3, FLAC, and M4A export available when ffmpeg is installed.
- Render large inputs in chunks.
- Use synthesizer-specific settings where available.
- Adjust rate, pitch, volume, voice, language, or variant where the selected synthesizer exposes those controls.
- Configure separate naming templates for single-render folders, single-render files, batch folders, and batch files.
- Optional post-render actions can open the output folder, play the rendered audio, or hide the summary.
- Use NVDA's add-on update channel for store-compatible updates.

## Documentation

- Full help: [`source/doc/en/readme.html`](source/doc/en/readme.html)

## Changes

- 1.1.2: Added a dedicated Orpheus Classic capture path. SoundWave now renders Orpheus Classic through its normal NVDA driver flow while capturing the generated audio directly, which avoids very short/truncated output from the generic NVDA capture path.
- 1.1.1: Improved long Google TTS renders by reusing one bridge instance, using smaller Google chunks, and retrying recoverable DevTools bridge failures. Added progress minimization/restoration and friendlier long-duration reporting. Restored generic NVDA synth settings after capture to reduce voice/language state leaks. Closes issue #5.
- 1.1.0: Major rendering update. Added folder/batch input, MP3/FLAC/M4A output, configurable output naming, optional dialog skipping, Google TTS dialog preview/render fixes, Pocket TTS direct rendering improvements including EOS sensitivity, Supertonic chunking, batch playlist playback, and split specialist synth support into separate modules.
- 1.0.4: Added googleTtsForNvda rendering support.
- 1.0.3: Added SAPI5 pitch support and changed SAPI5 WAV rendering to use the selected voice's default SAPI output format where available.
- 1.0.2: Added pitch and volume controls where available, improved numeric keyboard adjustment, remembered render details state, added voice names to suggested filenames, and improved dialog help/keyboard access.
- 1.0.1: Aligned update handling with NVDA Add-on Store distribution.
- 1.0.0: Initial release.

## Source Code

- Extracted source for this build: [`source/`](source/)
- Main plugin: [`source/globalPlugins/soundWave.py`](source/globalPlugins/soundWave.py)

## Install

1. Download `soundWave.nvda-addon` from Releases.
2. In NVDA, open Add-on Manager and choose Install.
3. Select the file and restart NVDA when prompted.

Latest packaged add-on: [`soundWave.nvda-addon`](./soundWave.nvda-addon)
