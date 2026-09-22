# JGBPL Wind Power Plant (50 MW Nilanga, Maharashtra) - Behavioral & Regulatory Dossier

## 1. Plant Identification & Metadata
- **Plant Name**: JGBPL
- **Display Name**: JGBPL (50MW Nilanga)
- **Portfolio / Ownership Status**: **Independent Standalone Asset** (Strictly separate from ILIOS POWER portfolio)
- **Plant Type**: Onshore Wind Farm
- **Location**: Nilanga, Latur District, Marathwada Region, Maharashtra, India
- **Coordinates**: Latitude `18.172244° N`, Longitude `76.776634° E`
- **Total Nameplate / AC Feed-in Capacity**: **50.0 MW** (50,000 kW)
- **Turbine Model**: **Envision EN182-5.0 MW**
  - **Number of Turbines**: 10 units (10 × 5,000 kW = 50,000 kW)
  - **Rotor Diameter**: **182.0 m** | **Swept Area**: 26,015 m² per turbine
  - **Hub Height**: **130.0 m**
  - **Specific Power**: Ultra-low specific power (~192.2 W/m²), engineered specifically for low-to-medium wind speed capture on the Deccan Plateau
  - **Cut-in Wind Speed ($v_{\text{cut-in}}$)**: 3.0 m/s
  - **Rated Wind Speed ($v_{\text{rated}}$)**: 10.5 m/s
  - **Cut-out Wind Speed ($v_{\text{cut-out}}$)**: 25.0 m/s
- **PPA Tariff**: **₹ 3.275 per kWh** (identical to Jewli MERC tariff)
- **Regulatory Jurisdiction**: Maharashtra Electricity Regulatory Commission (MERC)
- **Metering Status**: Non-Meter Virtual Site (`has_meter: false`, `is_virtual: true`)
  - Real-time schedules are synthesized through multi-agency Numerical Weather Prediction (NWP) ensembles (ECMWF, GFS, ICON, DWD) coupled with empirical aerodynamic and diurnal transfer calibration.

---

## 2. Regulatory Framework: MERC 10% Two-Sided Penalty Mandate

### 2.1 The Two-Sided Tolerance Band Rule
Under the Maharashtra Electricity Regulatory Commission (Forecasting, Scheduling and Deviation Settlement Mechanism for Wind and Solar Generating Stations) Regulations:
- **Available Capacity ($AC$)**: **50.0 MW** (50,000 kW).
- **MERC Tolerance Band**: **$\pm 10.0\%$ of Available Capacity** = **$\pm 5.00\text{ MW}$** ($\pm 5,000\text{ kW}$) (symmetric two-sided band identical to Jewli's 10% MERC rule).
- **Zero Penalty Corridor**: 
  $$\text{Safe Corridor} = [\text{Actual} - 5.00\text{ MW}, \;\; \text{Actual} + 5.00\text{ MW}]$$
- **Symmetric Enforcement**:
  - **Over-forecasting** ($\text{Schedule} > \text{Actual} + 5.00\text{ MW}$): Generating less than scheduled incurs DSM penalties on the shortfall energy.
  - **Under-forecasting** ($\text{Schedule} < \text{Actual} - 5.00\text{ MW}$): Generating more than scheduled **also incurs DSM penalties** on the un-scheduled injection.
  - **Scheduling Directive**: There is no "free" conservative safety margin. The scheduler must center forecasts precisely along the expected physical generation trajectory to prevent crossing either the upper or lower boundary.

### 2.2 Deviation Slabs & Settlement Mechanics
For any 15-minute time block $b$, let deviation $\Delta P_b = |\text{Schedule}_b - \text{Actual}_b|$.
Deviations beyond the 10% (5.0 MW) threshold are settled according to the MERC tiered slab schedule:

$$\text{Energy per block (kWh)} = \Delta P_b (\text{MW}) \times 1,000 \times 0.25\text{ h}$$
$$\text{Penalty (₹)} = \text{Energy (kWh)} \times ₹ 3.275/\text{kWh} \times \text{Slab Multiplier}$$

| Deviation Range (% of AC) | Absolute Deviation Range | Slab Penalty Multiplier | Effective Penalty Rate (₹/kWh) |
|---|---|---|---|
| **$\le 10\%$** | **$0.00\text{ MW}$ to $5.00\text{ MW}$** | **$0.0\%$** | **₹ 0.0000** (Zero Penalty Zone) |
| **$10\% - 15\%$** | **$5.00\text{ MW}$ to $7.50\text{ MW}$** | **$10.0\%$** | **₹ 0.3275** |
| **$15\% - 25\%$** | **$7.50\text{ MW}$ to $12.50\text{ MW}$** | **$20.0\%$** | **₹ 0.6550** |
| **$> 25\%$** | **$> 12.50\text{ MW}$** | **$30.0\%$** | **₹ 0.9825** |

### 2.3 Intra-Day Revision Schedule & Effective Timings (Identical to Jewli)
In accordance with MERC regulations, 16 intra-day revisions are permitted per day (every 90 minutes), taking effect after a 45-minute gate-closure freeze lag (3 time blocks):

| Revision # | Revision Target Time (IST) | Effective Time (IST) | Effective Block | Revisable Horizon |
|---|---|---|---|---|
| **Rev 1** | **01:15** | **02:00** | Block 9 | Blocks 9 to 96 (88 blocks) |
| **Rev 2** | **02:45** | **03:30** | Block 15 | Blocks 15 to 96 (82 blocks) |
| **Rev 3** | **04:15** | **05:00** | Block 21 | Blocks 21 to 96 (76 blocks) |
| **Rev 4** | **05:45** | **06:30** | Block 27 | Blocks 27 to 96 (70 blocks) |
| **Rev 5** | **07:15** | **08:00** | Block 33 | Blocks 33 to 96 (64 blocks) |
| **Rev 6** | **08:45** | **09:30** | Block 39 | Blocks 39 to 96 (58 blocks) |
| **Rev 7** | **10:15** | **11:00** | Block 45 | Blocks 45 to 96 (52 blocks) |
| **Rev 8** | **11:45** | **12:30** | Block 51 | Blocks 51 to 96 (46 blocks) |
| **Rev 9** | **13:15** | **14:00** | Block 57 | Blocks 57 to 96 (40 blocks) |
| **Rev 10** | **14:45** | **15:30** | Block 63 | Blocks 63 to 96 (34 blocks) |
| **Rev 11** | **16:15** | **17:00** | Block 69 | Blocks 69 to 96 (28 blocks) |
| **Rev 12** | **17:45** | **18:30** | Block 75 | Blocks 75 to 96 (22 blocks) |
| **Rev 13** | **19:15** | **20:00** | Block 81 | Blocks 81 to 96 (16 blocks) |
| **Rev 14** | **20:45** | **21:30** | Block 87 | Blocks 87 to 96 (10 blocks) |
| **Rev 15** | **22:15** | **23:00** | Block 93 | Blocks 93 to 96 (4 blocks) |
| **Rev 16** | **23:45** | **00:30 (next day)** | Block 3 | Blocks 3 to 96 (94 blocks) |

---

## 3. Empirical 30-Day Schedule Analysis & Plant Behavior

### 3.1 Dataset Overview
- **Analysis Period**: August 23, 2026, 00:00 IST to September 22, 2026, 23:45 IST (31 full operational days).
- **Total Time Blocks**: **2,976 continuous 15-minute intervals** (744.0 operational hours).
- **Missing Intervals**: 0 blocks (100% complete data continuity).
- **Overall Energy Scheduled**: **13,849.47 MWh**.
- **Mean Generation**: **18.61 MW** (18,608.6 kW).
- **Median Generation**: **15.62 MW** (15,620.0 kW).
- **Peak Generation**: **43.00 MW** (86.0% of rated capacity).
- **Minimum Floor**: **0.00 MW** (observed once on 2026-09-20 during plant outage; typical low-wind baseline is **6.00 MW**).
- **Standard Deviation**: **10.96 MW** (showing strong synoptic day-to-day dynamic range).
- **Capacity Utilization Factor (CUF)**: **37.22%** overall.

---

## 4. 24-Hour Diurnal Behavioral Regimes

Physical analysis of the 31-day data demonstrates that JGBPL Nilanga follows a distinctive 4-phase diurnal boundary layer pattern shaped by the Deccan Plateau topography and monsoon boundary layer dynamics:

```
 Generation (MW)
  50 |
  40 |               * * * *                                               * * *
  30 |         * * *         * * *                                   * * *
  20 | * * * *                     * * *                       * * *
  10 |                                   * * * * * * * * * * *
   0 +---------------------------------------------------------------------------
     00:00           06:00         09:00       12:00       17:30       22:00 24:00
     [--- Phase 1: Nocturnal Jet ---] [P2: Decay] [P3: Midday Lull] [P4: Evening Ramp]
```

### Phase Summary Table

| Operational Phase | Time Window (IST) | Blocks | Mean MW | Median MW | Min MW | Max MW | P10 MW | P90 MW | Phase CUF |
|---|---|---|---|---|---|---|---|---|---|
| **Phase 1: Nocturnal Jet & Morning Plateau** | **22:00 – 08:30** | 1,302 | **21.78 MW** | 21.11 MW | 4.50 MW | 42.46 MW | 6.50 MW | 37.99 MW | **43.56%** |
| **Phase 2: Convective Morning Decay** | **08:30 – 11:00** | 310 | **18.87 MW** | 15.53 MW | 4.50 MW | 40.00 MW | 6.50 MW | 33.97 MW | **37.73%** |
| **Phase 3: Midday Thermal Boundary Lull** | **11:00 – 17:30** | 806 | **15.67 MW** | 12.00 MW | 0.00 MW | 40.00 MW | 6.00 MW | 33.00 MW | **31.33%** |
| **Phase 4: Evening Decoupling & Jet Ramp** | **17:30 – 22:00** | 558 | **15.34 MW** | 13.27 MW | 5.50 MW | 43.00 MW | 6.64 MW | 30.00 MW | **30.69%** |

---

### Detailed Phase Dynamics & Physics

#### Phase 1: Nocturnal Low-Level Jet & Morning Sustained Plateau (22:00 to 08:30 IST)
- **Generation Performance**: Sustained high generation averaging **21.78 MW** (CUF 43.56%), with peaks reaching **42.46 MW**.
- **Diurnal Peak Window**: The highest mean generation of the entire 24-hour cycle occurs in the early morning between **06:00 and 08:00 IST**, peaking at **23.58 MW (Hour 7)**.
- **Physical Mechanism**: 
  - Radiative cooling of the Deccan surface creates a robust ground-based temperature inversion layer.
  - This inversion decouples the air mass above 100m from surface friction, allowing the regional geostrophic wind to accelerate into a nocturnal Low-Level Jet (LLJ).
  - Because the Envision EN182 turbines stand at **130 m hub height**, their large 182m rotor disks intercept the core of this LLJ throughout the night and early morning.
- **Key Scheduling Rule**: Wind production does NOT halt at night. Night and early morning hours represent peak revenue generation. Under no circumstances should nighttime wind schedules be zeroed or depressed.

#### Phase 2: Convective Morning Decay (08:30 to 11:00 IST)
- **Generation Performance**: Smooth downward descent from **23.58 MW at 08:00 down to 17.80 MW by 10:00**, with median dropping from 26.34 MW to 11.11 MW.
- **Physical Mechanism**: 
  - Solar insolation heats the soil, creating vertical convective thermal plumes.
  - These thermals penetrate and erode the nocturnal inversion layer, transferring high-altitude momentum down towards the surface while inducing heavy vertical eddy turbulence.
  - This turbulence creates aerodynamic drag that decelerates the hub-height wind speed.
- **Key Scheduling Rule**: Expect an orderly decay of ~2.5 MW per hour during this window. Avoid holding high nocturnal plateau forecasts past 08:30 IST.

#### Phase 3: Midday Thermal Boundary Lull (11:00 to 17:30 IST)
- **Generation Performance**: Suppressed generation averaging **15.67 MW** (CUF 31.33%), reaching the diurnal trough of **13.17 MW between 17:00 and 18:00 IST**.
- **Physical Mechanism**: 
  - Maximum convective mixing depth is reached. High surface roughness and thermal friction dominate the planetary boundary layer.
  - However, because the Envision EN182-5.0 MW has an ultra-low specific power (rotor area of 26,015 m² per 5 MW), the plant still captures substantial energy even in modest 5.0–6.5 m/s midday winds, sustaining a healthy 12–16 MW output.
- **Key Scheduling Rule**: Maintain a realistic midday baseline. The diurnal trough consistently occurs between 16:45 and 17:45 IST; do not artificially depress below the 6.0 MW baseline floor unless synoptic NWP predicts calm conditions (< 3 m/s).

#### Phase 4: Evening Decoupling & Nocturnal Ramp (17:30 to 22:00 IST)
- **Generation Performance**: Upward ramp transitioning from the diurnal low of **13.17 MW at 17:00** through **15.04 MW at 19:00**, **17.38 MW at 21:00**, to **18.99 MW at 22:00**.
- **Physical Mechanism**: 
  - Solar radiation terminates. Surface sensible heat flux drops to zero and reverses sign.
  - Convective turbulence rapidly dissipates within 30–45 minutes.
  - The decoupled boundary layer allows the Low-Level Jet to reconstitute, steadily accelerating hub-height wind speeds back towards nocturnal velocities.
- **Key Scheduling Rule**: Operators often lag behind this ramp. The model should initiate the upward ramp at 17:45–18:15 IST, ensuring that the forecast tracks the physical wind recovery without under-forecasting penalties.

---

## 5. Hourly Profile Statistics (00:00 to 23:00 IST)

The following table provides the 24-hour empirical baseline derived from all 31 operational days:

| Hour (IST) | Time Window | Mean (MW) | Median (MW) | Min (MW) | Max (MW) | P10 (MW) | P90 (MW) | CUF (%) | Diurnal Multiplier |
|---|---|---|---|---|---|---|---|---|---|
| **00** | 00:00 – 01:00 | 21.64 | 20.00 | 6.00 | 42.11 | 7.10 | 38.71 | 43.28% | 1.163 |
| **01** | 01:00 – 02:00 | 21.09 | 17.93 | 6.00 | 41.46 | 7.10 | 40.07 | 42.17% | 1.133 |
| **02** | 02:00 – 03:00 | 21.94 | 20.11 | 6.00 | 41.46 | 6.50 | 40.11 | 43.88% | 1.179 |
| **03** | 03:00 – 04:00 | 23.07 | 22.76 | 6.00 | 41.46 | 6.50 | 37.11 | 46.14% | 1.240 |
| **04** | 04:00 – 05:00 | 21.86 | 21.88 | 4.50 | 41.46 | 6.50 | 37.40 | 43.72% | 1.175 |
| **05** | 05:00 – 06:00 | 21.76 | 22.57 | 4.50 | 39.49 | 6.50 | 35.52 | 43.52% | 1.169 |
| **06** | 06:00 – 07:00 | 23.18 | 25.53 | 4.50 | 38.87 | 6.50 | 36.95 | 46.37% | 1.246 |
| **07** | 07:00 – 08:00 | **23.58** | **26.34** | 4.50 | 40.00 | 6.50 | 37.58 | **47.16%** | **1.267 (Peak)** |
| **08** | 08:00 – 09:00 | 21.27 | 20.72 | 4.50 | 40.00 | 6.50 | 35.65 | 42.54% | 1.143 |
| **09** | 09:00 – 10:00 | 19.01 | 15.72 | 4.50 | 40.00 | 6.50 | 34.59 | 38.02% | 1.021 |
| **10** | 10:00 – 11:00 | 17.80 | 11.11 | 4.50 | 40.00 | 6.29 | 33.51 | 35.59% | 0.956 |
| **11** | 11:00 – 12:00 | 17.29 | 12.95 | 0.00 | 40.00 | 6.00 | 33.09 | 34.57% | 0.929 |
| **12** | 12:00 – 13:00 | 16.42 | 14.30 | 0.00 | 40.00 | 6.00 | 33.43 | 32.84% | 0.882 |
| **13** | 13:00 – 14:00 | 16.07 | 13.24 | 4.50 | 40.00 | 6.00 | 34.71 | 32.15% | 0.864 |
| **14** | 14:00 – 15:00 | 15.08 | 12.00 | 3.13 | 35.60 | 6.00 | 33.42 | 30.17% | 0.811 |
| **15** | 15:00 – 16:00 | 15.27 | 11.75 | 4.26 | 36.50 | 6.00 | 32.71 | 30.54% | 0.821 |
| **16** | 16:00 – 17:00 | 15.15 | 12.25 | 5.50 | 35.00 | 6.00 | 31.40 | 30.31% | 0.814 |
| **17** | 17:00 – 18:00 | **13.17** | **10.70** | 5.50 | 34.50 | 6.00 | 27.64 | **26.35%** | **0.708 (Trough)** |
| **18** | 18:00 – 19:00 | 13.87 | 11.88 | 5.50 | 41.50 | 6.00 | 27.50 | 27.74% | 0.745 |
| **19** | 19:00 – 20:00 | 15.04 | 14.25 | 6.00 | 43.00 | 6.00 | 30.10 | 30.09% | 0.808 |
| **20** | 20:00 – 21:00 | 16.12 | 15.20 | 6.00 | 41.46 | 8.15 | 26.80 | 32.25% | 0.866 |
| **21** | 21:00 – 22:00 | 17.38 | 15.00 | 6.00 | 41.46 | 6.70 | 32.08 | 34.76% | 0.934 |
| **22** | 22:00 – 23:00 | 18.99 | 15.11 | 6.00 | 41.46 | 7.10 | 35.08 | 37.99% | 1.021 |
| **23** | 23:00 – 24:00 | 20.69 | 17.11 | 6.00 | 42.46 | 6.50 | 40.11 | 41.39% | 1.112 |

---

## 6. Synoptic Regimes & Monthly Volatility (31 Days)

The 31-day analysis captures two prominent weather patterns typical of late-monsoon conditions in Marathwada:

### 6.1 Active Monsoon Wind Surges (Late August)
- **Dates**: August 25 to August 29, 2026.
- **Daily Energy**: **669 MWh to 846 MWh per day**.
- **Daily CUF**: **55.8% to 70.5%**.
- **Characteristics**: Sustained daytime and nighttime generation above 30 MW, peaking at 40–43 MW. The nocturnal jet is reinforced by deep monsoon pressure gradients.

### 6.2 Break-Monsoon / Low-Wind Regimes (Mid September)
- **Dates**: September 17 to September 21, 2026.
- **Daily Energy**: **145 MWh to 204 MWh per day**.
- **Daily CUF**: **12.1% to 17.0%**.
- **Characteristics**: Light regional breezes. Daytime output drops to 6.0–8.0 MW, with weak evening ramps recovering only to 10–12 MW.

---

## 7. Operator Habits & Discretization Analysis

Review of the manual schedule reveals several operational stepping habits:
1. **Strong Discretization**:
   - **53.6%** of values are exact multiples of 100 kW.
   - **39.2%** of values are exact multiples of 500 kW.
   - **29.4%** of values are whole megawatt steps (1000 kW multiples).
2. **Floor Clamping**:
   - During low-wind periods, human operators rigidly clamped the schedule to exactly **6,000 kW (6.0 MW)** or **7,100 kW (7.1 MW)** for dozens of consecutive blocks, avoiding near-zero schedules.
3. **Plateau Holding**:
   - In steady conditions, operators held flat values (e.g. 23,110 kW, 25,110 kW, 29,110 kW) across 4 to 8 blocks (1 to 2 hours) before making an incremental step.
4. **Ramp Rates**:
   - **Mean Absolute 15-Minute Ramp**: **0.90 MW**.
   - **95th Percentile Ramp**: **5.00 MW**.
   - **99th Percentile Ramp**: **12.13 MW**.
   - Large step revisions typically occur when an operator adjusts the schedule upon receiving new intra-day forecast updates.

---

## 8. Automated Scheduler Configuration & Implementation

Based on this empirical dossier, the automated scheduler is configured with the following parameters in [`schedule/plant_profiles/JGBPL.json`](file:///d:/14%20sept%20intellis/schedule/plant_profiles/JGBPL.json) and [`schedule/plant_profiles/jgbpl_calibrated_multipliers.json`](file:///d:/14%20sept%20intellis/schedule/plant_profiles/jgbpl_calibrated_multipliers.json):

1. **Turbine Power Curve**: Calibrated Envision EN182-5.0 MW curve with aerodynamic cut-in at 3.0 m/s and rated power at 10.5 m/s.
2. **Hub Height Scaling**: Shear scaling $\alpha = 0.143$ from 100m to 130.0m hub height:
   $$v_{\text{hub}} = v_{100\text{m}} \times \left(\frac{130.0}{100.0}\right)^{0.143} = v_{100\text{m}} \times 1.0383$$
3. **Air Density Correction**: Density scaling $\rho(P, T) = \frac{P}{R_{\text{spec}} T}$ applied continuously across all 96 blocks.
4. **Diurnal Multipliers**: The 96-block transfer multipliers $M(b)$ are integrated directly into [`load_site_calibrated_multipliers`](file:///d:/14%20sept%20intellis/schedule/modules/weather/wind_ensemble.py#L120-L150) in `wind_ensemble.py`.
5. **Two-Sided Safety Band**: Target corridor centered strictly within $[\text{Actual} - 5.00\text{ MW}, \; \text{Actual} + 5.00\text{ MW}]$.
