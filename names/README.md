# Game-specific switch names

Place a `<rom>.json` file here for each game ROM you want to work with.
This file maps switch numbers to human-readable names, enabling the synthetic
recording skill to use named references (e.g. `"left_ramp"`) instead of
raw switch numbers.

The file format is a JSON object mapping switch number (as a string) to name:
```json
{
  "11": "start_button",
  "16": "left_ramp",
  "17": "right_ramp"
}
```

To generate a starter file for a new ROM, run a replay and inspect the
`schemas/switches/<rom>.discovered.json` output, then promote it here.
