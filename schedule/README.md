# Industrial AI Solar Power Forecasting Pipeline (2-Stage Architecture)

An automated, cyber-physical solar power forecasting system engineered for utility-scale solar plants. Compliant with **Indian Central Electricity Regulatory Commission (CERC)** and **State Deviation Settlement Mechanism (DSM)** regulations within the strict $\pm 15\%$ tolerance band.

---

## High-Level 2-Stage Pipeline Architecture

```mermaid
flowchart TD
    %% Styling
    classDef stageBox fill:#0F172A,stroke:#3B82F6,stroke-width:2px,color:#FFFFFF;
    classDef inputGroup fill:#1E293B,stroke:#64748B,stroke-width:1.5px,color:#E2E8F0;
    classDef streamBox fill:#0369A1,stroke:#38BDF8,stroke-width:1.5px,color:#FFFFFF;
    classDef arbiterBox fill:#6D28D9,stroke:#A855F7,stroke-width:2px,color:#FFFFFF;
    classDef outputBox fill:#047857,stroke:#10B981,stroke-width:2px,color:#FFFFFF;
    classDef triggerBox fill:#B45309,stroke:#F59E0B,stroke-width:1.5px,color:#FFFFFF;

    subgraph TRIGGER["Automated Trigger Layer (AWS EventBridge)"]
        TB["Cron Trigger (Every 15-90 Mins)<br/>05:15, 06:45, 08:15, 09:45, 11:15, 12:45, 14:15, 15:45 IST"]
    end

    subgraph STAGE1["STAGE 1: Physics & Real-Time SCADA Anchor Engine"]
        direction TB
        subgraph S1_INPUTS["Live SCADA & Physical Inputs"]
            I1["1. Live SCADA Inverter Readings<br/>• Real-time 15-min generation up to t₀ (MW)<br/>• Live Performance Ratio: η = SCADA / ClearSky"]
            I2["2. PVLib Clear-Sky Solar Geometry<br/>• Solar elevation (α) & zenith (θ_z)<br/>• Plane-of-Array Irradiance (GHI, DNI, DHI)"]
            I3["3. Plant Static Characteristics<br/>• AC / DC Capacity (MW)<br/>• Module tilt angle & orientation<br/>• Inverter clipping thresholds"]
            I4["4. Sandia PV Cell Temperature (T_cell)<br/>• Ambient temp & wind speed<br/>• Silicon thermal derating (-0.38%/°C)"]
        end
        
        S1_ENGINE["Physics & Meter Calibration Core<br/>• Computes theoretical clear-sky envelope<br/>• Applies real-time ground momentum calibration<br/>• Handles dawn inverter wake-up logic (α < 20°)"]
        S1_OUTPUT["Stage 1 Output:<br/>Baseline Physics Anchor Forecast (MW)<br/>(12 x 15-minute blocks for next 3 hours)"]

        I1 --> S1_ENGINE
        I2 --> S1_ENGINE
        I3 --> S1_ENGINE
        I4 --> S1_ENGINE
        S1_ENGINE --> S1_OUTPUT
    end

    subgraph STAGE2["STAGE 2: Cognitive Tri-Stream Weather & AI Arbitration"]
        direction TB
        
        subgraph WEATHER_STREAMS["Tri-Stream Weather Intelligence"]
            W1["Weather Stream 1: Deterministic<br/>ECMWF 9 km High-Resolution<br/>• Single Best-Match GHI & DNI<br/>• Microclimate rain & curve shape"]
            W2["Weather Stream 2: Super-Ensemble<br/>91-Member Multi-Model Ensemble<br/>• 51 ECMWF + 40 DWD ICON members<br/>• Uncertainty Spread (σ)<br/>• Conservative P40 Penalty Defense Floor"]
            W3["Weather Stream 3: Atmospheric Deep-Scan<br/>Open-Meteo Premium Multi-Agency<br/>• 5-Agency Consensus (ECMWF, ICON, GFS, JMA, CMC)<br/>• CAPE Convective Thunderstorm Index (J/kg)<br/>• CAMS Aerosol / Haze Attenuation (AOD 550nm)<br/>• Optical Cloud Transmissivity (UV/UV_clear)<br/>• 15-Min Native Sunshine Duration Fraction"]
        end

        ARBITER["Cognitive AI Fusion Arbiter (LLM / Neural Engine)<br/>• Blends Stage 1 Physics Anchor with Tri-Stream Weather<br/>• Dynamic SCADA Ground Blending (70% Ground / 30% Weather)<br/>• Downside Risk Protection using Stream 2 P40 Floor<br/>• Preemptive Thunderstorm & Dust Haze Attenuation"]

        SAFETY["CERC Regulatory & Ramp Guardrails<br/>• Clamps to [0, AC Capacity MW]<br/>• Grid ramp-rate smoothing filter<br/>• Enforces strict CERC ±15% tolerance compliance"]

        W1 --> ARBITER
        W2 --> ARBITER
        W3 --> ARBITER
        ARBITER --> SAFETY
    end

    %% Workflow Connections
    TB ==>|"Triggers Serverless Lambda"| S1_ENGINE
    S1_OUTPUT ==>|"Feeds Baseline Physics Anchor"| ARBITER

    FINAL["FINAL SCHEDULE OUTPUT<br/>5-Column Regulatory CSV<br/>(Block, Time, Plant Name, Base Forecast, Revised Forecast)"]
    S3_STORE[("AWS S3 Data Lake<br/>s3://ai-forecasting-storage-429694361053/<br/>generated/PLANT/YYYY-MM-DD/")]

    SAFETY ==> FINAL
    FINAL ==> S3_STORE

    %% Apply Styles
    class STAGE1,STAGE2 stageBox;
    class S1_INPUTS,WEATHER_STREAMS inputGroup;
    class W1,W2,W3 streamBox;
    class ARBITER arbiterBox;
    class FINAL,S1_OUTPUT outputBox;
    class TB,TRIGGER triggerBox;
```

---

## Step-by-Step Forecast Generation Process

### Step 0: EventBridge Schedule Trigger
* **Frequencies**: Fired automatically at revision checkpoints (`05:15`, `06:45`, `08:15`, `09:45`, `11:15`, `12:45`, `14:15`, `15:45 IST`).
* **Target Horizon**: 12 continuous 15-minute time blocks (next 3 hours).

### Step 1: Live SCADA Ingestion & Ground Calibration
* Fetches the latest 15-minute generation records up to the revision cutoff ($t_0$).
* Calculates the **Live Performance Ratio**:
  $$\eta_{\text{live}} = \frac{\text{Actual SCADA Power at } t_0}{\text{Theoretical Clear-Sky Power at } t_0}$$

### Step 2: Astronomical Geometry & PVLib Plane-of-Array Physics
* Calculates exact solar elevation angle ($\alpha_s$), zenith ($\theta_z$), and azimuth.
* Translates DNI and DHI to **Plane-of-Array (POA) Irradiance** ($\text{W/m}^2$) based on plant tilt ($\beta$) and orientation ($\gamma_{\text{panel}}$).

### Step 3: Sandia Thermal Loss & Stage 1 Base Anchor
* Ingests ambient temperature ($T_{\text{ambient}}$) and wind speed ($v$).
* Computes cell temperature ($T_{\text{cell}}$) and applies monocrystalline silicon derating ($\gamma = -0.38\% / ^\circ\text{C}$):
  $$T_{\text{cell}} = T_{\text{ambient}} + \text{POA} \cdot \exp(-a - b \cdot v)$$
  $$\mathbf{P}_{\text{anchor}} = \min\left(P_{\text{AC, max}}, \, P_{\text{DC}} \cdot \frac{\text{POA}_{\text{clearsky}}}{1000} \cdot (1 + \gamma (T_{\text{cell}} - 25^\circ\text{C})) \cdot \eta_{\text{live}}\right)$$

### Step 4: Parallel Fetch of Tri-Stream Weather Intelligence
* **Stream 1 (Deterministic)**: ECMWF 9 km High-Resolution deterministic GHI, precipitation, and cloud levels.
* **Stream 2 (Super-Ensemble)**: 91-Member Multi-Model Ensemble (51 ECMWF + 40 DWD ICON). Computes spread $\sigma$ and the **Conservative P40 Risk Floor** to eliminate DSM over-forecasting penalties.
* **Stream 3 (Deep-Scan)**: 5-Agency Consensus (ECMWF, ICON, GFS, JMA, CMC), CAPE convective thunderstorm index ($\text{J/kg}$), CAMS Aerosol Optical Depth (AOD 550nm), and 15-min native sunshine duration.

### Step 5: Cognitive AI Fusion & Arbitration Decision Matrix
The AI Arbiter evaluates the multi-stream evidence and arbitrates the optimal generation curve:

```text
                          ┌───────────────────────────┐
                          │   Stage 1 Physics Anchor  │
                          └─────────────┬─────────────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             ▼                          ▼                          ▼
   [Scenario A: Clear Sky]     [Scenario B: Morning Ramp]  [Scenario C: Volatile Storm]
   • High Agreement (>90%)     • Solar Elev: 15° - 45°     • High Spread (σ > 150 W/m²)
   • Spread σ < 40 W/m²        • Clear ground conditions   • CAPE > 1000 J/kg
   • CAPE < 500 J/kg           • Fast inverter ramping     • Cloud Cover > 60%
             │                          │                          │
             ▼                          ▼                          ▼
   Follow 100% Physics Anchor  Blend 70% SCADA Ground      Clamp Down to Stream 2 P40
   (Full Generation Ceiling)   + 30% Weather Trajectory    (Penalty Protection Floor)
```

### Step 6: Regulatory Safety Clamping & CERC $\pm 15\%$ Guardrails
* Hard physical limits enforced: $0.0 \le P \le P_{\text{AC, max}}$.
* Ramp-rate smoothing filter applied between consecutive 15-minute intervals.
* Early morning dawn inverter awakening threshold verified ($\alpha_s < 20.0^\circ$).

### Step 7: Schedule Formatting & AWS S3 Dispatch
* Merges the 12 revised blocks into the day's master 96-block schedule.
* Formats as the standard 5-column CSV:
  ```csv
  Block,Time,Plant Name,Base Forecast (MW),Revised Forecast (MW)
  24,12:00,KASIPET,14.850,14.250
  25,12:15,KASIPET,14.900,14.300
  ```
* Uploads directly to AWS S3:
  `s3://ai-forecasting-storage-429694361053/generated/<PLANT>/<DATE>/`

---

## Production Multi-Plant Deployment Matrix

All 5 solar power plants execute using this identical, containerized engine on AWS Lambda:

| Plant Name | AC Capacity | DC Capacity | Tilt Angle | Latitude | Longitude |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **SIRMOUR** | 5.1 MW | 5.48 MW | 20.0° | 24.562530 | 75.091403 |
| **KASIPET** | 15.0 MW | 16.50 MW | 15.0° | 19.039439 | 79.436917 |
| **BHUPALPALLY** | 10.0 MW | 11.005 MW | 15.0° | 18.447931 | 79.877263 |
| **KOTHAGUDEM** | 10.0 MW | 11.20 MW | 15.0° | 17.525009 | 80.612501 |
| **OSEPL** | 20.0 MW | 26.00 MW | 20.0° | 21.145800 | 79.088200 |

---

## Project Directory Layout

```text
schedule/
├── config.py                               # Single source of truth for plant profiles and weights
├── run_pipeline.py                         # End-to-end 2-stage execution orchestrator
├── modules/
│   ├── llm/
│   │   └── predictor.py                    # AI Fusion Arbiter & Decision Matrix prompt builder
│   ├── weather/
│   │   ├── weather_fusion.py               # Tri-stream weather fusion engine
│   │   ├── ecmwf_weather.py                # Stream 1: ECMWF 9 km deterministic model
│   │   ├── openmeteo_ensemble.py           # Stream 2: 91-member multi-model super-ensemble
│   │   └── premium_stream3.py              # Stream 3: 5-agency consensus, CAPE, & CAMS AOD
│   ├── physics/
│   │   └── physics_anchor.py               # PVLib clear-sky solar geometry & Sandia cell temp
│   └── feedback/
│       └── daily_feedback.py               # Evening SCADA ground-truth reconciliation
├── Dockerfile.shared-all                   # Container image specification for AWS Lambda
└── README.md                               # This technical specification document
```

---

## AWS Serverless Architecture
* **Compute**: AWS Lambda (Containerized Python 3.11, 2048 MB memory).
* **Execution Time**: Sub-10 seconds average per revision run.
* **Storage**: AWS S3 Bucket `ai-forecasting-storage-429694361053`.
* **Automation**: AWS EventBridge Scheduler Rules.
