# Jewli Wind Power Plant (100.8 MW, Maharashtra) - Behavioral & Regulatory Dossier

## 1. Plant Identification & Metadata
- **Plant Name**: JEWLI
- **Plant Type**: Onshore Wind
- **Location**: Jewali, Dharashiv (Osmanabad) District, Maharashtra, India
- **Coordinates**: Latitude `17.87562° N`, Longitude `76.36388° E`
- **Total Nameplate / AC Feed-in Capacity**: **100.8 MW** (100,800 kW)
- **Turbines**: 28 × Siemens Gamesa SG 3.6-145 (3,600 kW each)
- **Rotor Diameter**: 145.0 m | **Swept Area**: 16,513 m²
- **Hub Height**: **133.5 m**
- **PPA Tariff**: **₹ 3.275 per kWh**
- **Regulatory Jurisdiction**: Maharashtra Electricity Regulatory Commission (MERC)

---

## 2. Regulatory Framework: MERC 10% Two-Sided Penalty Band

### 2.1 The Two-Sided Tolerance Band Rule
Under MERC Forecasting & Scheduling Regulations:
- **Tolerance Band**: **±10.0% of Available Capacity** = **±10.08 MW** (±10,080 kW).
- **Zero Penalty Corridor**: `[Actual - 10.08 MW, Actual + 10.08 MW]`.
- **Symmetric Enforcement**:
  - **Over-forecasting** (`Schedule > Actual + 10.08 MW`): Incurs DSM penalty on excess deviation.
  - **Under-forecasting** (`Schedule < Actual - 10.08 MW`): **ALSO incurs DSM penalty** on shortfall deviation.
  - Unlike some state schemes that forgive under-injection or treat it lightly, MERC enforces strict penalties on both over- and under-predictions!

### 2.2 Penalty Slabs & Calculation Mechanics
Deviations beyond the 10% (10.08 MW) threshold in each 15-minute time block are penalized as follows:

$$\text{Energy per block (kWh)} = \text{Deviation (MW)} \times 1,000 \times 0.25\text{ h}$$
$$\text{Penalty (₹)} = \text{Energy (kWh)} \times ₹ 3.275/\text{kWh} \times \text{Slab Multiplier}$$

| Deviation Range | Absolute Deviation (MW) | Slab Multiplier | Effective Penalty Rate (₹/kWh) |
|---|---|---|---|
| **≤ 10%** | **0.00 to 10.08 MW** | **0.0%** | **₹ 0.0000** (Zero Penalty Corridor) |
| **10% to 15%** | **10.08 to 15.12 MW** | **10.0%** | **₹ 0.3275** |
| **15% to 25%** | **15.12 to 25.20 MW** | **20.0%** | **₹ 0.6550** |
| **> 25%** | **> 25.20 MW** | **30.0%** | **₹ 0.9825** |

### 2.3 Concrete Scheduling Examples (Actual = 60.00 MW)
- **Allowed Safe Corridor**: `[49.92 MW, 70.08 MW]`
- **Case 1: Safe Prediction (Schedule = 68.00 MW)**
  - Error = $+8.00\text{ MW}$ (within $+10.08\text{ MW}$).
  - **Penalty = ₹ 0.00**.
- **Case 2: Over-Forecasting (Schedule = 75.00 MW)**
  - Error = $+15.00\text{ MW}$.
  - Excess deviation beyond 10% = $15.00 - 10.08 = 4.92\text{ MW}$ (in 10-15% slab).
  - Energy = $4,920\text{ kW} \times 0.25\text{ h} = 1,230\text{ kWh}$.
  - **Penalty = ₹ 402.83**.
- **Case 3: Under-Forecasting (Schedule = 40.00 MW)**
  - Error = $-20.00\text{ MW}$.
  - Shortfall deviation beyond 10% = $20.00 - 10.08 = 9.92\text{ MW}$.
  - Slab 1 (10-15% = 5.04 MW): $5,040 \times 0.25 \times 0.3275 = ₹ 412.65$.
  - Slab 2 (15-25% = 4.88 MW): $4,880 \times 0.25 \times 0.6550 = ₹ 799.10$.
  - **Total Penalty = ₹ 1,211.75**.
- **Core Takeaway for Scheduler / LLM**:
  > **DO NOT** slash the forecast conservatively to "play safe" against over-injection, because falling below Actual - 10.08 MW triggers severe under-forecasting penalties! The optimal strategy is staying tightly centered inside `[Actual - 10.08 MW, Actual + 10.08 MW]`.

---

## 3. Meteorological & Empirical Diurnal Regimes (from 48-Day SCADA Analysis)

Analysis of 4,608 historical 15-minute intervals (August 1 to September 17, 2026) reveals 4 daily regimes:

```
 Generation (MW)
  100 |     * * * * * * * *                                     * * * * * * *
   80 |   *                 *                                 *
   60 |  *                   *                               *
   40 | *                     *                             *
   20 |                        *   - - - - - - - - - - - - *
    0 |--------------------------* * * * * * * * * * * * * ------------------
      00:00                 06:00                 12:00   18:00            24:00
      [--- Nocturnal LLJ ---] [Decay] [-- Day Lull --] [Ramp] [Nocturnal LLJ]
```

### Regime 1: Nocturnal Low-Level Jet (LLJ) (20:00 to 05:30 IST)
- **Generation**: Sustained **50.0 to 95.0 MW** (peaking at 94.95 MW).
- **Physics**: Strong nocturnal temperature inversion decouples boundary layer friction, producing a high-speed nocturnal jet at 100–150m above ground.
- **Rule**: Wind does NOT sleep. Night generation is **peak production**. NEVER zero out night blocks.

### Regime 2: Morning Thermal Decoupling (05:30 to 08:30 IST)
- **Generation**: Steep decay from **~70.0 MW down to ~15.0 MW**.
- **Physics**: Solar radiation warms the ground, inducing vertical convective thermals that mix and erode the nocturnal jet.
- **Rule**: Track the downward trend quickly. Do not hold high nocturnal values into daylight.

### Regime 3: Daytime Thermal Boundary Lull (08:30 to 18:15 IST)
- **Generation**: Low baseline of **0.5 to 7.0 MW**.
- **Physics**: Deep turbulent mixing creates high surface drag and low hub-height wind speeds.
- **Note**: During dead calm periods, negative meter values ($-100$ to $-350\text{ kW}$) reflect auxiliary transformer and turbine station loads.
- **Rule**: Keep daytime schedules low (1.0 to 5.0 MW) during sunny calm weather.

### Regime 4: Evening Transition Ramp (18:15 to 19:45 IST)
- **Generation**: Dramatic jump from **~4.0 MW to >65.0 MW** within 30–45 minutes.
- **Physics**: Solar heating ends; ground rapidly cools; turbulence vanishes, and the Low-Level Jet re-accelerates.
- **Scheduling Trap**: Human operators frequently miss ramp onset by 30–60 minutes, leading to massive excess or shortfall penalties. Monitor 17:30–18:00 anemometer readings to time the up-ramp correctly.

---

## 4. Summary of Scheduler Directives for Jewli
1. **Target**: Keep forecast strictly within `[Actual - 10.08 MW, Actual + 10.08 MW]`.
2. **Symmetry**: Guard equally against over-forecasting and under-forecasting.
3. **Hub Height**: Always scale NWP wind speeds to **133.5 m** using wind shear coefficient $\alpha = 0.143$.
4. **24-Hour Execution**: Run 96 continuous 15-minute blocks without daytime-only assumptions.
