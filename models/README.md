# models/

Put the wakeword model here as `hey_robbie.onnx`. The server loads it from
this directory (bind-mounted by `compose.example.yaml`; override the path
with `ROBBIE_WAKE_MODEL`). It is loaded when the station connects, so
dropping a file in or swapping it needs no rebuild — just reconnect the Pi.

The model is **not** part of this repository: wakeword models come with
terms that allow personal use only, so you fetch your own. Two ways:

- **Download (the default):** pick a wake word from the openWakeWord
  community library at <https://openwakeword.com/library> and download its
  ONNX file. Use is subject to the provider's terms (personal use).
- **Train your own:** the training center on the same site (free, account
  required) trains a wake word from a text prompt and exports the same ONNX
  format. Do this if you want a name the library doesn't have.

Choose two to three syllables and avoid everyday words.

**Rename the downloaded file to `hey_robbie.onnx`** whatever the wake word
is — the file stem is the key the server looks up (`WAKE_KEY` in
`server/duplex.py`, `wakeword` in `server/config/config.toml`). Renaming is
enough; the assistant then simply answers to the name you chose.

`*.onnx` files in this directory are git-ignored.
