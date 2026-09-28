# Engineering & Mathematical Blueprint: Intellis AI 2.0

This technical guide documents the core physics, mathematical equations, and deterministic algorithms powering the renewable forecasting engine.

---

## 1. Solar Clear-Sky Geometry & Irradiance

Solar power potential is bounded by extraterrestrial radiation, sun elevation, panel orientation, and atmospheric attenuation.

### Astronomical Elevation Angle
For latitude $\phi$, solar declination $\delta$, and hour angle $\omega$:
$$\sin(\alpha) = \sin(\phi) \sin(\delta) + \cos(\phi) \cos(\delta) \cos(\omega)$$
$$\text{Zenith Angle: } \theta_z = 90^\circ - \alpha$$

### Plane-of-Array (POA) Clear-Sky Model
Using the analytical Ineichen/Haurwitz clear-sky envelope:
$$\text{POA}_{\text{cs}}(b) = 980.0 \times \Big[\max\Big(0.0, \ \cos\big(h \times \frac{\pi}{12}\big)\Big)\Big]^{1.15} \quad (W/m^2)$$
where $h = |b - 48.5| \times 0.25$ represents hours from solar noon ($12:15\text{ PM}$).

### Photovoltaic Cell Thermal Loss Derating
Silicon photovoltaic cells experience efficiency losses as module temperatures exceed $25^\circ\text{C}$ (NOCT temperature response):
$$T_{\text{cell}} = T_{\text{ambient}} + \text{POA} \times 0.031 \quad (^\circ\text{C})$$
$$\text{Thermal Factor: } \eta_T = \text{clip}\Big(1.0 - 0.004 \times (T_{\text{cell}} - 25.0), \ 0.82, \ 1.06\Big)$$
$$\text{Clear-Sky MW} = \text{POA} \times \text{Plant Transfer Ratio} \times \eta_T$$

---

## 2. Intraday SCADA Persistence & Handover Decoupling

Atmospheric cloud shadow autocorrelation $R(\tau)$ decays rapidly as cloud fields advect across the plant:
$$R(\tau) \approx \exp\Big(-\frac{\tau}{\tau_0}\Big) \quad \text{where } \tau_0 \approx 20\text{--}30\text{ minutes}$$

To bridge ground SCADA telemetry with multi-agency weather models without causing persistent over/under-scheduling errors, the dispatch horizon is segmented relative to observation time:

```text
Observation (curr_idx)
      │
      ├── [elapsed_blocks <= 1 (+00 to +15 min)]: 100% Meter-Adjusted (k_t_obs + trend)
      ├── [elapsed_blocks == 2 (+15 to +30 min)]: 100% Meter-Adjusted (k_t_obs + 0.5 * trend)
      ├── [elapsed_blocks == 3 (+30 to +45 min)]: 50% Meter + 50% NWP Transition Bridge
      └── [elapsed_blocks >= 4 (> 45 min)]: 100% Pure NWP Weather Models
```

### Clearness Index ($k_t$) & Trend Vector
$$k_{t,\text{obs}} = \frac{\text{Metered Generation (MW)}}{\text{Clear-Sky MW}}$$
$$\text{Cloud Momentum: } \frac{dk_t}{dt} = k_{t}(T) - k_{t}(T - 15\text{ min})$$
$$\text{trend\_momentum} = \text{clip}\Big(\frac{dk_t}{dt} \times 1.5, \ -0.25, \ +0.20\Big)$$

---

## 3. The Four Deterministic Guardrails

Before any AI-adjusted generation number is saved to the final schedule, it must pass 4 hardcoded physical laws:

### Guardrail 1: Solar Geometry Hard Cutoff
$$\text{Schedule}(b) = 0.0\text{ MW} \quad \forall \ b \in [1, 23] \cup [77, 96] \quad (\text{Before 06:00 and After 19:15})$$

### Guardrail 2: Clear-Sky Negative Cut Lockout
Under confirmed clear sky ($k_t \ge 0.85$ or satellite cloud cover $\le 30\%$):
$$\text{Predicted MW} \ge \text{Clear-Sky Physics Baseline MW}$$
*Prevents AI models from arbitrarily slashing generation during bright blue sky intervals.*

### Guardrail 3: Overcast Optical Cloud Ceiling
Under confirmed overcast / monsoonal conditions ($k_t < 0.50$ or cloud cover $\ge 75\%$):
$$\text{Predicted MW} \le \max\Big(\text{Baseline MW} \times 1.10, \ 25\% \times \text{AC Capacity}\Big)$$
*Prevents AI models from hallucinating impossible power spikes during cloudy days.*

### Guardrail 4: Grid Code Ramp-Rate Continuity Filter
Between consecutive 15-minute dispatch blocks:
$$\Delta\text{MW}_{\max} = \max\Big(0.50\text{ MW}, \ 10\% \times \text{Plant AC Capacity}\Big)$$
$$\text{MW}_t = \text{clip}\Big(\text{MW}_{t}, \ \text{MW}_{t-1} - \Delta\text{MW}_{\max}, \ \text{MW}_{t-1} + \Delta\text{MW}_{\max}\Big)$$
*Ensures the schedule is smooth and complies with SLDC grid stability codes.*

---

## 4. Wind Energy Physics (Jewli 100.8 MW)

### Hub-Height Wind Speed Extrapolation
Power law wind shear profile converts surface anemometer data to $120\text{ m}$ hub height:
$$v_{\text{hub}} = v_{10} \times \left(\frac{z_{\text{hub}}}{z_{10}}\right)^\alpha \quad \text{where } \alpha \approx 0.143 \text{ (neutral atmosphere)}$$

### Air Density Scaling
Standard power curves assume sea-level air density ($\rho_0 = 1.225\text{ kg/m}^3$). Jewli operates at altitude ($\approx 500\text{ m}$):
$$\rho = \frac{P_{\text{baro}}}{R_{\text{specific}} \times T_{\text{kelvin}}} \approx 1.16\text{ kg/m}^3$$
$$\text{Corrected Wind Speed: } v_{\text{effective}} = v_{\text{hub}} \times \left(\frac{\rho}{\rho_0}\right)^{1/3}$$

### Diurnal Slot Calibration
Models (ECMWF IFS, GFS, ICON-EU) are selected dynamically for 3 diurnal operating windows:
1. **Nocturnal Low-Level Jet (LLJ)**: Blocks 81 to 22 ($20:00\text{ to }05:30$) — high continuous output ($50\text{--}95\text{ MW}$).
2. **Morning Decoupling**: Blocks 23 to 34 ($05:30\text{ to }08:30$) — thermal erosion steep ramp-down.
3. **Daytime Thermal Lull**: Blocks 35 to 73 ($08:30\text{ to }18:15$) — low convective output ($0.5\text{--}7.0\text{ MW}$).
