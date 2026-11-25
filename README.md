# Leica M11-D: exposure lock fix for firmware 2.6.4

On the M11-D, a half-press of the shutter button shows the exposure lock dot.
The camera then takes one more light reading and locks that one.
If you point the camera somewhere else in that time, the photo gets the wrong exposure.

This repo explains the cause and has a patch for firmware 2.6.4. The actual work was done by an LLM.

| File | What it is |
|---|---|
| `m11-d_2.6.4.patch` | The patch. It changes 13 instructions in the camera's main program. |
| `apply.py` | Applies the patch to Leica's firmware file. It needs Python 3 and nothing else. |

Leica's firmware is not in this repo. You download it from Leica.

Leica did not make or approve this patch.
A changed firmware can leave you with a camera that does not start.
Read [Risks](#risks) before you install anything.

It _did_ work for me: my M11-D runs this firmware, and the bug's gone.

## The bug

1. Half-press the shutter button. The lock dot shows in the viewfinder.
2. Right after the press, point the camera at something brighter or darker.
   You have up to about 0.3 seconds for this. The late reading comes that soon after the press.
   Waiting for over half a second will re-meter the first scene and will lock the already displayed value, and nothing will look wrong.
3. The shutter speed in the viewfinder changes one more time. Then it stays.
4. The photo is exposed for that last reading. It is not the reading you saw when you pressed.

The bug does not show on every try.
The window is the longest when the press lands in the 300 ms pause between metering rounds.
If it lands while a round of 2 frames is running, the next reading comes almost at once and there is no time to move.

The M11 does the same when its screen is off.
With live view on (rear screen or EVF) the bug does not show.

## The cause

The addresses below are for the M11-D main program in firmware 2.6.4.
The main program is part 83 of the firmware file.
It is 32-bit ARM code and the camera loads it at `0x41000000`.

1. A half-press sets the camera setting "AE lock" (id `0x204`) to 1. The dot shows at once.
2. The exposure task sees that the setting changed. It posts message `0x44100004` to its own queue.
3. When the task handles that message, it sets a flag to 1. The firmware's debug text calls the flag "Ignore AE Lock".
4. While the flag is 1, the lock is not applied. A new reading replaces the old one.
5. The next metered frame sets the flag back to 0. From then on the lock holds.

So the reading that gets locked is the first one taken after the half-press.

How long that takes depends on live view:

- **Live view on.** The sensor runs all the time. The flag goes back to 0 a few frames later.
- **Live view off.** The camera meters in rounds. One round is 2 frames. A pause of 300 ms follows.
  A half-press restarts a running pause.
  The next reading can come more than 300 ms after the dot.

An M11-D without an EVF never has live view on.

Below, "stock" means Leica's own firmware.

```
Stock:  half-press -> dot -> wait for the next reading -> that reading is locked
Fixed:  half-press -> dot -> the reading in the viewfinder is locked
```

## The fix

The fix changes step 2.
The stock firmware always posts the message.
The fixed firmware skips the message when all three of these are true:

| Test | Meaning |
|---|---|
| The "AE lock" setting is 1 | This is a half-press. A release sets it to 0. |
| The "has a reading" byte is not 0 | Metering has finished at least one round since it was switched on. |
| "Ignore AE Lock" is 0 | A reading has been used since the last release. |

With no message, the flag stays 0. The lock holds from the half-press on.
The locked value is the one the viewfinder shows at that moment.

In every other case the fixed firmware posts the message, as the stock firmware does.

Why each test is there:

- **Test 1.** A release also changes the setting. It must work as before.
- **Test 2.** After power-on, wake-up or a photo, the camera has no current reading. Stock waits for one. The fix keeps that.
  The byte already exists in the firmware. Stock uses it to decide whether a photo has to wait for a first reading.
- **Test 3.** After a release the camera keeps the old locked value until the next reading comes in.
  Without this test, a quick second half-press would lock that old value again.

### Before and after

In C-like form:

```c
// stock
case AE_LOCK_SETTING_CHANGED:
    post_to_own_queue(0x44100004);        // the handler sets "Ignore AE Lock" to 1
    debug_print(...);

// fixed
case AE_LOCK_SETTING_CHANGED:
    if (get_setting(AE_LOCK) != 1         // a release
        || has_a_reading == 0             // no finished metering round yet
        || get_ignore_ae_lock() != 0)     // no reading used since the last release
        post_to_own_queue(0x44100004);    // same as stock
```

The same as ARM instructions.
The stock firmware calls the "post" function. Then it prints a debug line, which takes 12 instructions.
The fix is written over the call and the print. That is 13 instructions, or 52 bytes.
No other byte of the main program changes.

Stock:

```
41166034  bl    0x41165a98        ; post message 0x44100004
41166038  movw  r3, #0xd464       ; from here on: print a debug line
4116603c  movt  r3, #0x430f
41166040  ldr   r3, [r3]
41166044  cmp   r3, #2
41166048  bhi   0x41166068
4116604c  ldr   r3, [r11, #-0x20]
41166050  ldr   r3, [r3, #4]
41166054  mov   r0, #2
41166058  movw  r1, #0xc844
4116605c  movt  r1, #0x4220
41166060  mov   r2, r3
41166064  bl    0x41111640
41166068  ...                     ; the rest of the handler
```

Fixed:

```
41166034  movw  r0, #0x204        ; r0 = id of the "AE lock" setting
41166038  bl    0x41135494        ; r0 = value of that setting
4116603c  cmp   r0, #1
41166040  bne   0x41166064        ; not 1: this is a release. Do what stock does
41166044  movw  r3, #0x7d74
41166048  movt  r3, #0x433a       ; r3 = 0x433a7d74, address of the "has a reading" byte
4116604c  ldrb  r3, [r3]
41166050  cmp   r3, #0
41166054  beq   0x41166064        ; no reading yet. Do what stock does
41166058  bl    0x4116bc2c        ; r0 = the "Ignore AE Lock" flag
4116605c  cmp   r0, #0
41166060  beq   0x41166068        ; flag is 0: skip the message. The lock holds
41166064  bl    0x41165a98        ; what stock does: post message 0x44100004
41166068  ...                     ; the rest of the handler, not changed
```

Places in the main program, if you want to read the code yourself:

| Address | What is there |
|---|---|
| `0x41165af8` | The exposure task |
| `0x41166034` | The changed place. The task gets here when the "AE lock" setting changes |
| `0x41165a98` | Posts message `0x44100004` to the task's own queue |
| `0x41165e40` | The task handles message `0x44100004` here |
| `0x4116bc3c` | Sets "Ignore AE Lock" |
| `0x4116bc2c` | Reads "Ignore AE Lock" |
| `0x41135494` | Reads a camera setting by id |
| `0x41167780` | Decides whether the lock applies to the next calculation |
| `0x433a7d74` | The "has a reading" byte |

### What changes when you use the camera

- **Metering has settled and you half-press.** The value in the viewfinder is locked. This is the fix.
- **You release and half-press again at once.** Same as stock. The lock takes the next reading.
  Wait about half a second after a release to get the fixed behaviour.
- **First half-press after power-on, wake-up or a photo.** Same as stock.
- **Live view on.** Same as stock.
- **The locked reading can be one metering round old.**
  If you swing to a new scene and half-press at once, you lock the old scene.

One known case is worse than stock:

- You release and half-press again, and both happen while the exposure task is busy with one message.
- The old lock then continues, as if you had not released. Stock would take a new reading.
- The task is estimated to be busy for a few milliseconds at most. This was not measured.
- To get out of it, release for longer and half-press again.

## Apply the patch

1. Download `LEICA_M11-D_2.6.4.FW` from Leica's
   [M11-D downloads page](https://leica-camera.com/en-int/photography/cameras/m/m11-d-black/downloads).
2. Run the script:

   ```sh
   python3 apply.py m11-d_2.6.4.patch LEICA_M11-D_2.6.4.FW
   ```

3. The script writes two files next to the firmware file:
   - `LEICA_M11-D_2.6.4_aelock.FW` is the firmware with the fix.
   - `LEICA_M11-D_2.6.4_back_to_stock.FW` takes a camera with the fix back to Leica's firmware.

The script checks SHA-256 sums:

- It stops if your file is not Leica's 2.6.4 file.
- It writes nothing unless both new files come out as listed here.

| File | SHA-256 |
|---|---|
| `LEICA_M11-D_2.6.4.FW` | `cfb9051b7b739baf7a450737df82d44698f572cc218d696c7d91ab8ccb829380` |
| `LEICA_M11-D_2.6.4_aelock.FW` | `0dadc9a2b6d31d65e1e06598fe59ba1e29f986074b43a4627c7944fb7889e41c` |
| `LEICA_M11-D_2.6.4_back_to_stock.FW` | `ffa1d93fc973a971f5cd831be5a3fb3650478368d8d9594a971775b0d354cd6e` |

The fixed file is, byte for byte, the file that was installed on the test camera.

## Install

The camera must already run firmware 2.6.4.
The fixed file is made so that a camera on any other version refuses it.

1. Charge the battery fully.
2. Copy `LEICA_M11-D_2.6.4_aelock.FW` to the top folder of an SD card. Leave no other `.FW` file on the card.
3. Switch the camera off and put the card in.
4. Hold the function button and switch the camera on.
5. The status LED and the self-timer LED flash red. The viewfinder shows `UP`.
6. Wait until the update is finished.

During the update:

- Do not switch the camera off.
- Do not take out the card, the battery or the lens.
- Do not touch the shutter button.

Steps 2 to 5 are Leica's own update steps. They are in the
[2.6.4 release notes](https://leica-camera.com/sites/default/files/EN_-2.6.4--M-Family-Release-Note.pdf).

After the update the camera reports version `2.6.4.1` over USB.

### Test it

1. Set the shutter speed dial to `A`. Wait a second so that metering is running.
2. Point at a bright scene and half-press.
3. Keep the half-press. Within about 0.3 seconds of the press, point at a dark scene.
   A later move shows no difference between the two firmwares.
4. Watch the shutter speed in the viewfinder. With the fix it does not change. With stock firmware it changes once.
5. Stock firmware does not do this on every try. Repeat the test several times before you decide that your camera does not have the bug.

### Go back to Leica's firmware

Use `LEICA_M11-D_2.6.4_back_to_stock.FW`. Install it the same way.

- A camera with the fix refuses Leica's own 2.6.4 file.
  The camera accepts that file only when it runs a lower version. The fixed firmware has the same version number as 2.6.4.
  This comes from reading the update code. It was not tried on a camera.
- The back-to-stock file holds Leica's packed firmware, byte for byte.
  Two numbers in its header are changed. They tell the camera to accept the file when it runs 2.6.4.
- This file was never installed on an M11-D.
  [matnetik's notes](https://github.com/matnetik/m11-firmware-notes) report that the same header change worked on one M11.
- A newer firmware from Leica has a higher version number. It should install as normal and replace the fix.

## Risks

- An update rewrites the camera's main program.
  If the new program does not start, there may be no way to install another one.
  [matnetik's notes](https://github.com/matnetik/m11-firmware-notes) say the M11 has no separate recovery updater.
- Do not let the power fail during the update. Use a full battery.
- The fixed file was installed on one M11-D, on 2026-11-19. That camera starts, and the lock works as described here.
  No other camera was tested.
- The back-to-stock file was never installed on a camera.
- The rare case under "What changes when you use the camera" was found by reading the code. It was not tried on a camera.

You install this at your own risk.

## The firmware file format

`apply.py` reads and writes this format.

**The file** is a 72-byte header followed by packed data.

| Offset | Content |
|---|---|
| `0x0c` | Size of the data after unpacking |
| `0x14` | Size of the packed data |
| `0x18` | MD5 of the packed data |
| `0x30` | Version of this firmware |
| `0x34`, `0x38` | Lowest and highest version the camera may run. Both are 0 in Leica's file |
| `0x3c` | Version text, 8 bytes. `2.6.4` in Leica's file |

**The packed data** starts with one byte that names the marker value. In 2.6.4 it is `0xA2`. After it:

- `marker, 0` is one plain byte with the marker's value.
- `marker, length, distance` copies `length` bytes, starting `distance` bytes back in the output.
- Any other byte is a plain byte.
- `length` and `distance` are stored 7 bits per byte, high bits first. A set top bit means one more byte follows.

**The unpacked data** is a table of 86 parts, followed by the parts.

- Each table entry holds the offset, the size and the MD5 of one part.
- The MD5 of the whole table is stored in front of the table.
- Part 83 is the main program.

**The camera's checks** before it writes anything:

- the camera model and the version numbers in the header
- the MD5 of the packed data
- the size after unpacking
- the MD5 of the table
- the MD5 of every part

No signature check was found.

**What `apply.py` does:**

1. It unpacks the data.
2. It checks the 13 stock words in part 83 and writes the fixed words.
3. It writes the new MD5 of part 83 and the new MD5 of the table.
4. It packs the data again.
   It keeps each of Leica's copy commands that still gives the right bytes.
   Where a command no longer does, it writes plain bytes.
   So the new file holds only Leica's own commands and plain bytes.
5. It writes the header: new size, new MD5, the version text `2.6.4.1`, and the version at `0x34` and `0x38`.

## The M11

The M11 on firmware 2.6.4 has the same bug when its screen is off.
The same code is at other addresses there.
This repo has no M11 patch. A fix for the M11 has not been tested on a camera.

## Sources

- [matnetik/m11-firmware-notes](https://github.com/matnetik/m11-firmware-notes): the firmware file format, and reports of changed firmware on an M11.
- Leica's [2.6.4 release notes](https://leica-camera.com/sites/default/files/EN_-2.6.4--M-Family-Release-Note.pdf): the update steps.

## License

The files in this repo are under the [MIT license](LICENSE).
Leica's firmware is not. It belongs to Leica.
