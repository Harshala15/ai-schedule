import pandas as pd
import numpy as np
import json
from pvlib.location import Location
from pvlib import irradiance

lat, lon = 24.077752, 75.337636
tilt, azimuth = 15.0, 188.0
ratio = 0.018408
ppa_rate = 6.97

loc = Location(lat, lon, tz='Asia/Kolkata')
times = pd.date_range('2026-09-14 00:00', '2026-09-14 23:45', freq='15min', tz='Asia/Kolkata')
sp = loc.get_solarposition(times)
zen = sp['apparent_zenith'].values
cos_zen = np.maximum(0.0, np.cos(np.radians(zen)))
clearsky = loc.get_clearsky(times, model='ineichen')
poa_cs = irradiance.get_total_irradiance(tilt, azimuth, sp['apparent_zenith'], sp['azimuth'], clearsky['dni'], clearsky['ghi'], clearsky['dhi'])
cs_gti = poa_cs['poa_global'].fillna(0.0).clip(lower=0.0).values
cs_mw = cs_gti * ratio

# Load meter data
meter_df = pd.read_csv('s3_downloads/GSNP/2026-09-14/raw/vedanjay/GSNP/2026-09-14/metered_data/GSPPL_FORECAST_2026-09-14.csv')
meter_mw = np.array([max(0.0, float(meter_df.loc[i, 'TVM Active Power'])/1000.0) if i < len(meter_df) else 0.0 for i in range(96)])

# Load SLDC Rev 4 schedule
sched_df = pd.read_excel('s3_downloads/GSNP/2026-09-14/Vedanjay SLDC Schedules/GSNP/2026-09-14/20260914_092803_DC_REG_2026-09-14_4.xlsx', skiprows=4, header=None)
sldc_mw = sched_df.iloc[2:98, 3].astype(float).values

# Load premium json
with open('openmeteo_premium_data/premium_gti_2026-09-14.json') as f:
    prem = json.load(f)

# Load ranked models
rank_df = pd.read_csv('openmeteo_api_data/all_91_models_ranked.csv')
top_pure = rank_df.head(5)['key'].tolist()
top_icon3 = rank_df[rank_df['label'].str.contains('ICON')].head(3)['key'].tolist()
top_ecmwf2 = rank_df[rank_df['label'].str.contains('ECMWF')].head(2)['key'].tolist()
diverse_keys = top_icon3 + top_ecmwf2

hourly_idx = np.arange(0, 24, 1.0)
b_idx = np.arange(0, 24, 0.25)

def get_native_gti(key):
    gti_k = key.replace('shortwave_radiation', 'global_tilted_irradiance')
    if gti_k in prem['hourly']:
        h_vals = [float(v) if v is not None else 0.0 for v in prem['hourly'][gti_k]]
        return np.interp(b_idx, hourly_idx, h_vals)
    dni_k = key.replace('shortwave_radiation', 'direct_normal_irradiance')
    sw_h = [float(v) if v is not None else 0.0 for v in prem['hourly'][key]]
    dni_h = [float(v) if v is not None else 0.0 for v in prem['hourly'][dni_k]]
    g_96 = np.interp(b_idx, hourly_idx, sw_h)
    d_96 = np.interp(b_idx, hourly_idx, dni_h)
    dh_96 = np.maximum(0.0, g_96 - d_96 * cos_zen)
    p = irradiance.get_total_irradiance(tilt, azimuth, sp['apparent_zenith'], sp['azimuth'], d_96, g_96, dh_96)
    return p['poa_global'].fillna(0.0).clip(lower=0.0).values

# 1. Pure Top 5
gti_pure = [get_native_gti(k) for k in top_pure]
mw_pure = np.clip(np.mean(gti_pure, axis=0) * ratio, 0.0, 20.0)

# 2. Diverse Top 5 (3 ICON + 2 ECMWF)
gti_div = [get_native_gti(k) for k in diverse_keys]
mw_div = np.clip(np.mean(gti_div, axis=0) * ratio, 0.0, 20.0)

# 3. Modification A: Diurnal Time-Segmented Blending
# Morning (blocks 24-40, 06:00-10:00): 75% ICON, 25% ECMWF
# Midday (blocks 41-58, 10:15-14:30): 35% ICON, 65% ECMWF
# Afternoon (blocks 59-76, 14:45-19:00): 60% ICON, 40% ECMWF
icon_mean = np.mean(gti_div[:3], axis=0) * ratio
ecmwf_mean = np.mean(gti_div[3:], axis=0) * ratio
mw_diurnal = []
for b in range(96):
    if b < 24 or b > 76:
        mw_diurnal.append(0.0)
    elif 24 <= b <= 40:
        mw_diurnal.append(0.75 * icon_mean[b] + 0.25 * ecmwf_mean[b])
    elif 41 <= b <= 58:
        mw_diurnal.append(0.35 * icon_mean[b] + 0.65 * ecmwf_mean[b])
    else:
        mw_diurnal.append(0.60 * icon_mean[b] + 0.40 * ecmwf_mean[b])
mw_diurnal = np.clip(np.array(mw_diurnal), 0.0, 20.0)

# 4. Modification B: Clear-Sky Index (kt) Averaging
kt_list = []
for g in gti_div:
    with np.errstate(divide='ignore', invalid='ignore'):
        kt = np.where(cs_gti > 20.0, g / cs_gti, 0.0)
    kt = np.nan_to_num(kt, nan=0.0, posinf=1.0, neginf=0.0)
    kt_list.append(np.clip(kt, 0.0, 1.2))
avg_kt = np.mean(kt_list, axis=0)
mw_kt = np.clip(avg_kt * cs_mw, 0.0, 20.0)

# 5. Modification C: Full 91-model P60 quantile
all_gti = [get_native_gti(k) for k in rank_df['key'].tolist()]
p60_gti = np.percentile(all_gti, 60, axis=0)
mw_p60 = np.clip(p60_gti * ratio, 0.0, 20.0)

def calc_metrics(pred):
    devs = np.abs(meter_mw - pred)
    mae = np.mean(devs[24:76]) # daylight blocks
    rmse = np.sqrt(np.mean((meter_mw[24:76] - pred[24:76])**2))
    tot_pen = 0.0
    safe = 0
    slab10 = 0
    slab20 = 0
    slab30 = 0
    for i in range(96):
        d = devs[i]
        if d <= 2.0:
            safe += 1
        elif d <= 4.0:
            slab10 += 1
            tot_pen += (d - 2.0) * 250.0 * 0.10 * ppa_rate
        elif d <= 6.0:
            slab20 += 1
            tot_pen += (2.0 * 250.0 * 0.10 * ppa_rate) + ((d - 4.0) * 250.0 * 0.20 * ppa_rate)
        else:
            slab30 += 1
            tot_pen += (2.0 * 250.0 * 0.10 * ppa_rate) + (2.0 * 250.0 * 0.20 * ppa_rate) + ((d - 6.0) * 250.0 * 0.30 * ppa_rate)
    return mae, rmse, tot_pen, safe, slab10, slab20, slab30

cases = [
    ('Submitted SLDC Rev 4 (Current Prod)', sldc_mw),
    ('Pure Top 5 (Single Family - All ICON)', mw_pure),
    ('Mod 1: Diverse Ensemble (3 ICON + 2 ECMWF)', mw_div),
    ('Mod 2: Clear-Sky Index (kt) Averaging', mw_kt),
    ('Mod 3: Diurnal Time-Segmented Blend', mw_diurnal),
    ('Mod 4: Full 91-Model P60 Asymmetric Risk', mw_p60),
]

print(f"{'Configuration':42s} | {'Day MAE':7s} | {'RMSE':7s} | {'Safe':4s} | {'10%':3s} | {'20%':3s} | {'>30%':4s} | {'Penalty (Rs)':12s} | {'Savings vs Sched':18s}")
print('-'*120)
base_pen = 0.0
for name, p in cases:
    mae, rmse, pen, safe, s10, s20, s30 = calc_metrics(p)
    if 'Submitted' in name:
        base_pen = pen
        sav_str = 'BASELINE'
    else:
        diff = base_pen - pen
        pct = (diff / base_pen) * 100
        sav_str = f"Rs. {diff:+8.2f} ({pct:4.1f}%)"
    print(f"{name:42s} | {mae:7.3f} | {rmse:7.3f} | {safe:4d} | {s10:3d} | {s20:3d} | {s30:4d} | Rs. {pen:9.2f} | {sav_str}")

# Print blockwise detail at key solar noon blocks (Blocks 44 to 56)
print('\n=== KEY SOLAR NOON BLOCKS COMPARISON (11:00 to 14:00 IST) ===')
print(f"{'Blk':3s} | {'Time':5s} | {'Actual (MW)':11s} | {'SLDC Rev4':10s} | {'Pure ICON':10s} | {'Diverse (3+2)':13s} | {'Diurnal Blend':13s} | {'kt Blend':10s}")
print('-'*90)
for b in range(44, 58):
    t_str = f"{b//4:02d}:{(b%4)*15:02d}"
    print(f"{b+1:3d} | {t_str:5s} | {meter_mw[b]:10.2f}  | {sldc_mw[b]:10.2f} | {mw_pure[b]:10.2f} | {mw_div[b]:12.2f}  | {mw_diurnal[b]:12.2f}  | {mw_kt[b]:10.2f}")
