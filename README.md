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
- Optionally apply post-render Sonic-style pitch processing to the finished audio.
- Configure separate naming templates for single-render folders, single-render files, batch folders, and batch files.
- Optional post-render actions can open the output folder, play the rendered audio, or hide the summary.
- Use NVDA's add-on update channel for store-compatible updates.

## Documentation

- Full help: [`source/doc/en/readme.html`](source/doc/en/readme.html)

## Changes

- 1.2.9: Added safe direct rendering for Loquendo TTS 7 through an isolated 32-bit helper, preserving voice settings and Loquendo inline controls such as expressive vocal effects. SoundWave no longer starts NVDA's separate speech host for Loquendo rendering, preventing no-audio attempts, temporary host-log floods, and host shutdown hangs. Generic capture now rejects other separate-host synthesizers that do not yet have a dedicated rendering path.
- 1.2.8: Added direct support for Dengjen Neural Voices while retaining Sonata compatibility and remembered voice selection. Improved long Samsung Galaxy Voices, Samsung TV Voices, and Nokia TTS rendering with engine-appropriate chunking and Nokia text normalization. Generic NVDA capture now records only the selected render synthesizer, preventing ordinary NVDA speech and sounds from entering output files or causing mixed-format failures.
- 1.2.7: Made optional NVDA synthesizer setting detection safe for bridged and proxied drivers, preventing unsupported settings such as Rate boost from stopping the options dialog. Closes issue #12.
- 1.2.6: Changed Escape and the progress window close control to hide an active render instead of cancelling it; only the Cancel button now cancels. Fixed cancellation cleanup so SoundWave does not remain incorrectly marked as already open. Long renders now time out only after five minutes without measurable progress, and Pocket TTS can recover from difficult model fragments through deeper adaptive splitting. A 2,990-character Pocket TTS render completed successfully in just over seven minutes during release testing.
- 1.2.5: Fixed the Sonata options dialog so Sonata capture opens and renders again. SoundWave now keeps one render workflow open at a time, raises the existing dialog when its command is pressed again, and brings the completion summary forward after automatically opening the output folder.
- 1.2.4: Added language selection for Keynote Gold/BestSpeech and generic NVDA synthesizers that expose a language setting. Pocket TTS render options now include its native 1 to 10 Flow steps quality control. Long Pocket TTS renders recover safely from the model's known zero-length transformer-state failure by retrying only the affected segment in smaller pieces, without retaining partial audio.
- 1.2.3: Updated Prose 2000 rendering to use the synth's native firmware Rate, Pitch and Volume controls. Removed inert Voice and Variant choices from the Prose options dialog while retaining compatibility with older Prose builds.
- 1.2.2: Updated the Polish and Slovak interface translations and manuals from issue #11. Added a release workflow that generates and attaches a current translation template (`soundWave.pot`) for translators. Closes issue #11.
- 1.2.1: Added safe, direct Prose 2000 rendering through its isolated host. Voice testing and final rendering no longer use soundWave's generic NVDA audio interception for Prose, preventing audio-hook conflicts and preserving Prose rate, Rate Boost, and volume settings.
- 1.2.0: Added optional post-render Sonic-style pitch processing, Polish and Slovak interface localization, Polish and Slovak manuals, and an output-folder write check before rendering starts. Closes issues #7 and #8. Based on PR #9 and PR #10.
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
