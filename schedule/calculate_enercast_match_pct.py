import boto3
import io
import pandas as pd
import numpy as np

s3 = boto3.client('s3', region_name='ap-south-1')
bucket = 'vedanjay-schedules-test-552496640388'
today = '2026-10-17'

all_plants = [
    'ANDAD', 'ANJANGOAN', 'BALAKWADA', 'BAMKHAL', 'BHUPALPALLY',
    'CHANDAWASA', 'CME', 'GUGARIYAKHEDI', 'JEWLI', 'JGBPL',
    'KASIPET', 'KOTHAGUDEM', 'NANDGAON', 'OSEPL', 'REWASEIT',
    'REWASPRNG', 'SAWDA', 'SIRMOUR'
]

print('=' * 95)
print(f'COMPREHENSIVE ENERCAST vs INTELLIS SCHEDULE MATCH EVALUATION ({today})')
print('=' * 95)
header = f'{"Plant":<15} | {"Intellis Peak":<13} | {"Enercast Peak":<13} | {"MAE (MW)":<10} | {"Correlation":<12} | {"Match %":<10}'
print(header)
print('-' * 95)

results = []

for p in all_plants:
    # 1. Download Intellis current_final_schedule or latest_schedule
    int_key = f'generated/vedanjay_ai_intellis/{p}/outputs/{today}/{p}_{today}_current_final_schedule.csv'
    try:
        r_int = s3.get_object(Bucket=bucket, Key=int_key)
        df_int = pd.read_csv(io.BytesIO(r_int['Body'].read()))
    except Exception:
        try:
            int_key = f'generated/vedanjay_ai_intellis/{p}/outputs/{today}/{today}_latest_schedule.csv'
            r_int = s3.get_object(Bucket=bucket, Key=int_key)
            df_int = pd.read_csv(io.BytesIO(r_int['Body'].read()))
        except Exception:
            continue

    # 2. Find latest Enercast file on S3
    ec_prefix = f'raw/vedanjay/{p}/{today}/enercast_data/intraday/'
    r_ec_list = s3.list_objects_v2(Bucket=bucket, Prefix=ec_prefix)
    ec_files = [o['Key'] for o in r_ec_list.get('Contents', []) if o['Key'].endswith('.csv')]
    if not ec_files:
        continue
    latest_ec_key = sorted(ec_files)[-1]
    r_ec = s3.get_object(Bucket=bucket, Key=latest_ec_key)
    
    raw_lines = r_ec['Body'].read().decode('utf-8', errors='ignore').splitlines()
    header_idx = 0
    for i, l in enumerate(raw_lines[:15]):
        u = l.upper()
        if 'BLK NO' in u or 'FROM TIME' in u or 'S. NO' in u or 'SCH_MW' in u or 'FROM' in u:
            header_idx = i
            break
            
    try:
        df_ec = pd.read_csv(io.StringIO('\n'.join(raw_lines[header_idx:])), on_bad_lines='skip')
    except Exception:
        continue
    
    # Identify forecast column correctly:
    # Prefer SCH_MW, or plant name column, or column containing forecast/schedule
    ec_col = None
    for c in df_ec.columns:
        c_str = str(c).strip().upper()
        if c_str in ('SCH_MW', 'SCHEDULE_MW', 'FORECAST_MW'):
            ec_col = c
            break
        elif p in c_str:
            ec_col = c
            break
            
    if not ec_col:
        for c in df_ec.columns:
            c_str = str(c).strip().upper()
            if 'AVAILAB' in c_str or 'AVC_MW' in c_str or 'CAPACITY' in c_str or 'BLK' in c_str or 'TIME' in c_str or 'DATE' in c_str or 'S. NO' in c_str:
                continue
            ec_col = c
            break

    if not ec_col:
        ec_col = df_ec.columns[-2] if len(df_ec.columns) >= 2 else df_ec.columns[-1]

    int_col = 'schedule_mw' if 'schedule_mw' in df_int.columns else 'intellis_mw'

    s_int = pd.to_numeric(df_int[int_col], errors='coerce').fillna(0.0).values
    s_ec = pd.to_numeric(df_ec[ec_col], errors='coerce').fillna(0.0).values

    min_len = min(len(s_int), len(s_ec))
    mw_int = s_int[:min_len]
    mw_ec = s_ec[:min_len]
    
    mask = (mw_int > 0.05) | (mw_ec > 0.05)
    if not any(mask):
        continue
    
    sub_int = mw_int[mask]
    sub_ec = mw_ec[mask]
    
    mae = float(np.mean(np.abs(sub_int - sub_ec)))
    std_int = np.std(sub_int)
    std_ec = np.std(sub_ec)
    if std_int > 1e-4 and std_ec > 1e-4:
        corr = float(np.corrcoef(sub_int, sub_ec)[0, 1])
    else:
        corr = 1.0 if mae < 0.1 else 0.0
    
    peak_ec = float(np.max(sub_ec))
    peak_int = float(np.max(sub_int))
    wmape = float(np.sum(np.abs(sub_int - sub_ec)) / np.sum(sub_ec)) if np.sum(sub_ec) > 0 else 0.0
    match_pct = max(0.0, (1.0 - wmape) * 100.0)

    print(f'{p:<15} | {peak_int:>7.2f} MW    | {peak_ec:>7.2f} MW    | {mae:>6.3f} MW | {corr:>10.4f}  | {match_pct:>8.1f}%')
    results.append({
        'plant': p,
        'int_peak': peak_int,
        'ec_peak': peak_ec,
        'mae': mae,
        'corr': corr,
        'match_pct': match_pct
    })

print('=' * 95)
avg_match = np.mean([r['match_pct'] for r in results])
avg_corr = np.mean([r['corr'] for r in results])
print(f'OVERALL AVERAGE ACROSS ALL {len(results)} PLANTS: Match = {avg_match:.1f}% | Correlation = {avg_corr:.4f}')
print('=' * 95)
