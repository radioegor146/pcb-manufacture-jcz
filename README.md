# pcb-manufacture-jcz

\[WIP\] PCB manufacturing using JCZ UV laser

*Currently, configuration shown here is TBD, do not use it*

Order of operation:

0. Convert your PCB production files using converter by reading and following [docs](CONVERTER.md)
1. Turn on chiller
2. Wait for it to reach 25°C
3. Turn 3 buttons on laser ON in top-to-bottom order
4. Remove safety cover (NOW THE LASER IS ARMED)
5. Connect laser to the PC
6. Set your laser head height based on your PCB stackup:
  - Single sided, 1.5mm, 35µm: `193mm`
7. Open Lightburn, configure it for your job:
  - For copper: 
    - Passes must be configured as `Type: Fill, Angle Increment: 45°, Interval: 0.04mm`
    - Single sided, 1.5mm, 35µm:
      1. N? passes of `Q-Pulse: 1ns, Frequency: 20kHz, Speed 100mm/s`
      2. 1 pass of `Q-Pulse: 1ns, Frequency: 30kHz, Speed 350mm/s`
  - For cuts (holes and edge): 
    - ???
8. Do your passes
9. Disconnect laser from PC
10. Attach safety cover (NOW THE LASER IS SAFE)
11. Turn 3 buttons on the laser OFF in bottom-to-top order
12. Turn off chiller
13. ???
14. PROFIT