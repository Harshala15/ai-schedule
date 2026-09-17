# Intellis Global Tilted Irradiance (GTI) Calculation Logic

## 1. Mathematical Formulation

Global Tilted Irradiance (GTI) is the total solar irradiance received by a solar module inclined at tilt angle $\beta$ and azimuth $\gamma$. Rather than predicting noisy raw Watts directly, Intellis decouples the calculation into physical geometry and atmospheric clearness:

$$\mathbf{GTI_{\text{Intellis}}(t) = K_t(t) \times GTI_{\text{clearsky}}(t)}$$

* **$GTI_{\text{clearsky}}(t)$**: Deterministic astronomical clear-sky Plane-of-Array potential calculated via PVLib.
* **$K_t(t) \in [0.05, 1.15]$**: Dimensionless **Atmospheric Clearness Index**, determined by the multi-model ensemble, live SCADA feedback, and OpenRouter LLM consensus arbitration.

---

## 2. The 7-Stage Calculation Pipeline

```
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 1: Astronomical Clear-Sky Scaffold (PVLib Ineichen Geometry)        │
 └─────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 2: 143-Member Global Weather Super-Ensemble (DWD, ECMWF, NOAA, GEM) │
 └─────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 3: 7-Day Exponential Decay Benchmark (tau = 3.5d) & Slot Quotas     │
 └─────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 4: Real-Time SCADA Telemetry Inversion (GTI_ground & Kt_actual)     │
 └─────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 5: OpenRouter LLM Consensus Arbitration (GPT-5.6 / Claude-3.7)      │
 └─────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 6: Closed-Loop SCADA Telemetry Relaxation (T+4 Nudge, tau = 45 min) │
 └─────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STAGE 7: Diurnal Spline Smoothing & Regulatory Clamping (Final GTI & MW)  │
 └───────────────────────────────────────────────────────────────────────────┘
```

---

## Stage-by-Stage Detailed Breakdown

### Stage 1: Astronomical Clear-Sky Scaffold ($GTI_{\text{clearsky}}$)
Every 15-minute block of the 96 daily blocks is computed using physical solar geometry:
1. **Solar Angles**: Using plant latitude ($\phi$), longitude ($\lambda$), and local standard time, PVLib computes:
   * Solar Zenith Angle ($\theta_z$)
   * Solar Azimuth Angle ($\gamma_s$)
   * Solar Elevation Angle ($\alpha = 90^\circ - \theta_z$)
2. **Clear-Sky Transposition**: Using Ineichen/Haurwitz clear-sky models with the plant's structural tilt ($\beta$) and surface azimuth ($\gamma_p$), the beam, diffuse, and ground components are transposed to the plane of array:
   $$GTI_{\text{clearsky}} = \text{POA}_{\text{global}}(\theta_z, \gamma_s, \beta, \gamma_p, DNI_{\text{clear}}, DHI_{\text{clear}}, GHI_{\text{clear}})$$
   *(At night or when $\alpha < 3.0^\circ$, $GTI_{\text{clearsky}}$ is strictly $0.00\text{ W/m}^2$)*.

---

### Stage 2: 143-Member Global Weather Super-Ensemble Ingestion
The engine queries 143 numerical weather members from Open-Meteo Premium configured to the plant's exact tilt and azimuth:
* **DWD ICON-Seamless (Germany)**: 40 members (13 km grid) — local convective cloud modeling.
* **ECMWF IFS-025 (Europe)**: 51 members (25 km grid) — global synoptic tracks and cloud thickness.
* **NOAA GEFS-025 (USA)**: 31 members (25 km grid) — fast morning boundary-layer dissipation.
* **Canada GEM (Canada)**: 21 members (39 km grid) — moisture front transitions.

**Lag Elimination**:
* Extracts native `global_tilted_irradiance_instant` to prevent the standard 30-minute phase-shift lag caused by hourly interval averages.

---

### Stage 3: 7-Day Exponential Decay Benchmark & Diurnal Slot Quotas
Every candidate member is evaluated against the plant's real pyranometer / meter POA over rolling 7 days:
1. **Exponential Time Weighting**:
   $$w_d = \exp\left(-\frac{d}{3.5}\right) \quad (d \in [1, 7] \text{ days})$$
2. **Atmospheric Regime Matching**:
   Classifies each day by clearness index $K_t$:
   * Overcast: $K_t < 0.50$
   * Mixed / Scattered: $0.50 \le K_t < 0.75$
   * Clear Sky: $K_t \ge 0.75$
   Days matching today's regime receive a $1.25\times$ analog weight bonus.
3. **Diurnal Tri-Engine Quota Selection**:
   Models are ranked and chosen **independently per diurnal slot**:
   * **Morning (06:00 – 10:00)**: 2 GEFS (fast morning sunrise ramp) + 2 ICON + 1 ECMWF
   * **Midday (10:00 – 14:00)**: 2 ICON (non-hydrostatic convection) + 2 ECMWF + 1 GEFS
   * **Afternoon (14:00 – 18:45)**: 2 ECMWF (synoptic clearing & thermal decay) + 2 ICON + 1 GEFS
4. **Bayesian Inverse-Variance Weights**:
   $$W_k = \frac{\frac{1}{\text{RMSE}_k}}{\sum_j \frac{1}{\text{RMSE}_j}}$$

---

### Stage 4: Real-Time SCADA Telemetry Inversion
At each revision gate ($T_{\text{rev}}$), the engine reads the active generation from the TVM SCADA meter ($P_{\text{meter}}$ in MW) and inverts it back into **ground-truth POA irradiance**:

$$GTI_{\text{actual}} = \frac{P_{\text{meter}}}{\text{Transfer Ratio} \times \eta_{\text{temp}}}$$

where:
* $\text{Transfer Ratio} = \frac{P_{\text{DC}} \times \text{PR}}{1000} \text{ MW per W/m}^2$
* $\eta_{\text{temp}} = 1.0 - 0.004 \times (T_{\text{cell}} - 25^\circ\text{C})$
* $K_t^{\text{meter}} = \frac{GTI_{\text{actual}}}{GTI_{\text{clearsky}}(T_{\text{rev}})}$

**Inverter AC Saturation & Clipping Lock**:
When $P_{\text{meter}} \ge 0.88 \cdot P_{\text{cap}}$ and solar elevation $\ge 35^\circ$, the inverters are operating at full electrical export capacity. The engine flags `is_inverter_clipped = True` and locks $K_t = 1.00$, preventing false downward cuts during solar noon.

---

### Stage 5: OpenRouter LLM Consensus Arbitration
The prompt receives:
1. **Live Ground Truth**: $P_{\text{meter}}$, inverted $GTI_{\text{actual}}$, live $K_t$, and clipping status.
2. **Slot Candidate Diagnostics**: The top 3 models for the active slot (e.g. ICON#13, ECMWF#09, GEFS#03) and their **last 60-minute live tracking bias** ($\Delta = GTI_{\text{model}} - GTI_{\text{actual}}$).
3. **LLM Decision**:
   * Evaluates which model tracks the actual ground condition.
   * If a model has a large bias ($> 50\text{ W/m}^2$), it zeroes out or heavily down-weights it.
   * Promotes the best-tracking model and outputs:
     * `winning_model`
     * `model_weights`
     * Consensus Clearness Index $K_t(t)$
     * `predicted_gti`

---

### Stage 6: Closed-Loop SCADA Telemetry Relaxation ($T+4$ Nudge)
To eliminate seam jumps at revision time, an exponential relaxation filter ($\tau = 45\text{ minutes}$) bridges Block 1 ($T+15\text{m}$) directly to current SCADA generation:

$$GTI(k) = \left(e^{-\frac{(k+1) \times 15}{\tau}}\right) \cdot GTI_{\text{projected\_meter}}(k) + \left(1 - e^{-\frac{(k+1) \times 15}{\tau}}\right) \cdot GTI_{\text{LLM}}(k)$$

* **Block 1 ($T+15\text{m}$)**: $72\%$ Ground Meter $+ 28\%$ LLM Model $\to$ **Zero seam jump error!**
* **Block 2 ($T+30\text{m}$)**: $51\%$ Ground Meter $+ 49\%$ LLM Model
* **Block 3 ($T+45\text{m}$)**: $37\%$ Ground Meter $+ 63\%$ LLM Model
* **Block 4+ ($T+60\text{m}$)**: Seamlessly follows the LLM-selected winning model trajectory.

---

### Stage 7: Diurnal Spline Smoothing & Regulatory Clamping
1. **Cosine Spline Slot Blending**:
   Transitions between Morning, Midday, and Afternoon models use a cosine window (Blocks 38–42 and 54–58) to eliminate step discontinuities.
2. **Physical & Diurnal Constraints**:
   * **Night**: If solar elevation $< 3.0^\circ$, $GTI = 0.00\text{ W/m}^2$.
   * **Dawn/Dusk**: Between $3.0^\circ$ and $7.5^\circ$, bounded to diffuse light ($25 - 50\text{ W/m}^2$).
   * **Ramp Monotonicity**: Strictly non-decreasing in morning ascent; strictly non-increasing in afternoon descent.
   * **AC Saturation Floor**: Preserves the midday convex plateau.
3. **Conversion to Scheduled MW**:
   $$P_{\text{Intellis}}(t) = \min\left(P_{\text{cap}}, \; GTI_{\text{Intellis}}(t) \times \text{Transfer Ratio} \times \eta_{\text{temp}}\right)$$
