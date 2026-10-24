# MicMix

Talk and play music into Discord through one microphone. MicMix mixes your mic with the sound of one app you pick (Spotify, a browser, your MP3 player), the way OBS's Application Audio Capture does. Game sounds, notifications and Discord itself stay out. A slider fades between your voice and the music.

![MicMix](docs/micmix.png)

For Windows 10 (version 2004 or later) and Windows 11. It is one Python file that uses only the standard library. MicMix itself needs no admin rights and changes no Windows setting.

## How it works

Only a driver can add a microphone to Windows, so MicMix sends its mix through [VB-CABLE](https://vb-audio.com/Cable/), a free virtual audio cable from VB-Audio. MicMix plays into **CABLE Input**, and Discord uses **CABLE Output** as its microphone.

## Install

1. **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/windows/). The installer's defaults are fine.
2. **VB-CABLE.** Download the driver pack from [vb-audio.com/Cable](https://vb-audio.com/Cable/), unzip it, right-click `VBCABLE_Setup_x64.exe`, choose **Run as administrator**, then click **Install Driver**. When it finishes, restart Windows.
3. **Check your Windows defaults.** The VB-CABLE installer can make the cable your default speakers and mic. Press **Win+R**, type `mmsys.cpl` and press Enter. On the Playback tab, right-click your real headphones or speakers and choose **Set as Default Device**, then **Set as Default Communication Device**. Then do the same for your real mic on the Recording tab.
4. **MicMix.** Click **Code**, then **Download ZIP**, on this page. Right-click the zip, choose **Extract All**, and extract it to a folder of your own, such as Documents. Don't use Program Files: MicMix saves your picks next to itself and can't save there. Open the extracted folder, then the folder inside it, and double-click `micmix.pyw` (Windows may show it as just `micmix`).

## Set up Discord

In Discord, open **User Settings**, then **Voice & Video**:

- **Input Device:** CABLE Output (VB-Audio Virtual Cable)
- **Input Profile:** Custom. The settings below only show up under Custom.
- **Noise Suppression:** None. **Echo Cancellation:** off. **Automatic Gain Control:** off. All three chop up music.
- **Input Sensitivity:** turn off "Automatically determine input sensitivity" and drag the slider to the far left. Otherwise quiet parts of songs cut out.

## Use

- **Microphone:** the mic you talk into. If you use NVIDIA Broadcast, pick its mic to keep its noise removal on your voice.
- **Music from:** the app your music plays in. Only that app goes in. A browser goes in with all its tabs, so don't pick the browser you run Discord in. The list shows apps that have sound open, as Windows' volume mixer does, so start playing if yours is missing. Below the apps are whole output devices, which take everything playing on that device (like OBS's Desktop Audio). If you pick one, keep Discord's output on a different device, or people hear themselves echo back.
- **Send to:** CABLE Input. MicMix picks it by itself once VB-CABLE is installed.
- **Slider:** left is voice only, the middle is both at full, and right is music only. Music under your voice usually sits around 10 to 20%.
- **Stop:** close the window; nothing keeps running. Discord then hears silence, so for calls without music, set Discord's Input Device back to your real mic.

MicMix saves your picks in `micmix.json` next to the app. If the music app isn't running, or its sound stops, MicMix keeps sending your voice and says so in its status line.

## Troubleshooting

- **No music in Discord.** Check that the app is playing. Turn off "exclusive mode" if your music app has it (TIDAL and foobar2000 do): it bypasses the Windows mixer, so nothing can capture the sound. If you restarted the music app, pick it again under Music from.
- **No sound on your PC since installing VB-CABLE, or the music echoes in Discord.** Windows is playing into the cable, so your apps reach Discord directly as well as through MicMix. Set your real headphones back as the default (Install, step 3).
- **"...would loop into itself."** The cable became one of your Windows default devices. Set your real devices back (Install, step 3).
- **Discord hears nothing.** Discord's Input Device must be CABLE Output, and MicMix must be open.

Your voice reaches Discord about 120 ms later than it would directly. That's buffering that keeps it from crackling.

## Test

Open the MicMix folder in File Explorer, type `cmd` in the address bar and press Enter, then run `py -m unittest test_micmix` to check the mixing math.

## License

Free to use, rights kept: see [LICENSE.txt](LICENSE.txt). VB-CABLE is VB-Audio's software under its own terms. MicMix doesn't include it.
