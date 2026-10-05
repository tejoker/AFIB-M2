import blpapi, json, sys, time, datetime, importlib.util
def native(el):
    if el.isArray(): return [native(v) if isinstance(v,blpapi.Element) else v for v in el.values()]
    if el.isComplexType(): return {str(v.name()):native(v) for v in [el.getElement(i) for i in range(el.numElements())]}
    try:
        v=el.getValue()
        return v.isoformat() if isinstance(v,(datetime.date,datetime.datetime)) else v
    except Exception: return None
opt=blpapi.SessionOptions();opt.setServerHost('localhost');opt.setServerPort(8194)
s=blpapi.Session(opt)
if not s.start(): raise RuntimeError('Session failed')
if not s.openService('//blp/refdata'): raise RuntimeError('Service failed')
svc=s.getService('//blp/refdata')
def request(securities,fields,kind='ReferenceDataRequest',params=None,overrides=None):
    req=svc.createRequest(kind)
    for sec in securities:req.append('securities',sec)
    for fld in fields:req.append('fields',fld)
    for k,v in (params or {}).items():req.set(k,v)
    for k,v in (overrides or {}).items():
        ov=req.getElement('overrides').appendElement();ov.setElement('fieldId',k);ov.setElement('value',v)
    s.sendRequest(req); result=[];deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        ev=s.nextEvent(1000)
        for msg in ev:
            d=native(msg.asElement())
            if 'securityData' in d:
                result.extend(d['securityData'] if isinstance(d['securityData'],list) else [d['securityData']])
            elif 'responseError' in d: result.append(d)
        if ev.eventType()==blpapi.Event.RESPONSE:return result
    raise TimeoutError('Bloomberg request timed out')

import numpy as np, pandas as pd, math, pathlib, collections
SAVE = '--save' in sys.argv
ROOT=pathlib.Path.cwd() / 'russell1000_liquidity_analysis'
REF_FIELDS=['NAME','ID_BB_GLOBAL_COMPANY','ID_BB_GLOBAL','GICS_SECTOR','GICS_INDUSTRY','GICS_SUB_INDUSTRY','COUNTRY_ISO','CRNCY','CUR_MKT_CAP','EQY_SH_OUT','EQY_FLOAT','PX_LAST','PX_BID','PX_ASK','LAST_UPDATE_DT','VOLUME_AVG_20D','VOLUME_AVG_3M','PE_RATIO','BEST_PE_RATIO','CURRENT_EV_TO_T12M_EBITDA','PX_TO_BOOK_RATIO','RETURN_ON_ASSET','RETURN_COM_EQY','PROF_MARGIN','SALES_GROWTH','NET_DEBT_TO_EBITDA','TRAIL_12M_NET_INC','BETA_RAW_OVERRIDABLE','BEST_SALES','BEST_EPS']
sector_names={10:'Energy',15:'Materials',20:'Industrials',25:'Consumer discretionary',30:'Consumer staples',35:'Health care',40:'Financials',45:'Information technology',50:'Communication services',55:'Utilities',60:'Real estate'}
def progress(x):print(x,flush=True)
universe=request(['RIY Index','SPX Index'],['NAME','INDX_MEMBERS','LAST_UPDATE_DT'])
def members(x):return {r['Member Ticker and Exchange Code'].rsplit(' ',1)[0]+' US Equity' for r in x['fieldData']['INDX_MEMBERS']}
russell=members(universe[0]);sp=members(universe[1]);allsec=sorted(russell|sp)
progress('UNIVERSE '+json.dumps({'russell_securities':len(russell),'sp_securities':len(sp),'union':len(allsec),'index_update_dates':[x['fieldData']['LAST_UPDATE_DT'] for x in universe]}))
rawref=[]
for i in range(0,len(allsec),100):
    rawref.extend(request(allsec[i:i+100],REF_FIELDS,overrides={'SCALING_FORMAT':'UNT','BEST_FPERIOD_OVERRIDE':'1BF'}))
rawforecast2=[]
for i in range(0,len(allsec),100):
    rawforecast2.extend(request(allsec[i:i+100],['BEST_SALES','BEST_EPS'],overrides={'SCALING_FORMAT':'UNT','BEST_FPERIOD_OVERRIDE':'2BF'}))
forecast2={r['security']:r.get('fieldData',{}) for r in rawforecast2}
errors=[{'security':r.get('security'),'error':r.get('securityError'),'fieldExceptions':r.get('fieldExceptions')} for r in rawref if r.get('securityError') or r.get('fieldExceptions')]
security_rows=[]
for r in rawref:
    row={'security':r.get('security'),**r.get('fieldData',{})}
    row['in_russell_security']=row['security'] in russell;row['in_sp_security']=row['security'] in sp
    row['company_id']=row.get('ID_BB_GLOBAL_COMPANY') or row['security']
    row['best_sales_2bf']=forecast2.get(row['security'],{}).get('BEST_SALES')
    row['best_eps_2bf']=forecast2.get(row['security'],{}).get('BEST_EPS')
    security_rows.append(row)
dfsec=pd.DataFrame(security_rows)
for f in REF_FIELDS:
    if f not in dfsec:dfsec[f]=np.nan
for f in REF_FIELDS:
    if f not in ['NAME','ID_BB_GLOBAL_COMPANY','ID_BB_GLOBAL','COUNTRY_ISO','CRNCY','LAST_UPDATE_DT']:
        dfsec[f]=pd.to_numeric(dfsec[f],errors='coerce')
companyflags=dfsec.groupby('company_id')[['in_russell_security','in_sp_security']].max()
# Choose the most active Russell security for an excluded company; prefer an S&P line for included companies.
dfsec['priority']=dfsec['in_sp_security'].astype(int)*2+dfsec['in_russell_security'].astype(int)
representatives=dfsec.sort_values(['priority','VOLUME_AVG_3M'],ascending=[False,False]).drop_duplicates('company_id').set_index('company_id')
df=representatives.drop(columns=['in_russell_security','in_sp_security']).join(companyflags).reset_index()
df=df.rename(columns={'in_russell_security':'in_russell','in_sp_security':'in_sp500'})
df['excluded']=(df['in_russell'] & ~df['in_sp500']).astype(int)
df['sector']=df['GICS_SECTOR'].map(sector_names)
df['market_cap_usd']=df['CUR_MKT_CAP']
df['float_shares']=df['EQY_FLOAT']*1e6
df['shares_out']=df['EQY_SH_OUT']*1e6
df['float_fraction']=df['float_shares']/df['shares_out']
df['forward_pe']=df['BEST_PE_RATIO']
df['forward_sales_growth_pct']=(pd.to_numeric(df['best_sales_2bf'],errors='coerce')/df['BEST_SALES']-1)*100
df.loc[df['BEST_SALES']<=0,'forward_sales_growth_pct']=np.nan
df['ev_ebitda']=df['CURRENT_EV_TO_T12M_EBITDA']
df['price_book']=df['PX_TO_BOOK_RATIO']
df['spread_bps_snapshot']=(df['PX_ASK']-df['PX_BID'])/((df['PX_ASK']+df['PX_BID'])/2)*1e4
df.loc[(df['PX_BID']<=0)|(df['PX_ASK']<df['PX_BID']), 'spread_bps_snapshot']=np.nan
querysec=df['security'].tolist()
rawhist=[]
for i in range(0,len(querysec),100):
    rawhist.extend(request(querysec[i:i+100],['PX_LAST','PX_VOLUME','TURNOVER'],'HistoricalDataRequest',{'startDate':'20260701','endDate':'20261002','periodicitySelection':'DAILY','adjustmentNormal':False,'adjustmentAbnormal':False,'adjustmentSplit':False,'adjustmentFollowDPDF':False}))
    progress('HISTORY '+str(min(i+100,len(querysec)))+'/'+str(len(querysec)))
hist=[]
for r in rawhist:
    for row in r.get('fieldData',[]):hist.append({'security':r['security'],**row})
h=pd.DataFrame(hist)
for f in ['PX_LAST','PX_VOLUME','TURNOVER']:h[f]=pd.to_numeric(h[f],errors='coerce')
h=h.sort_values(['security','date'])
# Obtain split-adjusted prices separately; keep actual trading volumes/traded values unadjusted.
rawadj=[]
for i in range(0,len(querysec),100):
    rawadj.extend(request(querysec[i:i+100],['PX_LAST'],'HistoricalDataRequest',{'startDate':'20260701','endDate':'20261002','periodicitySelection':'DAILY','adjustmentNormal':False,'adjustmentAbnormal':False,'adjustmentSplit':True,'adjustmentFollowDPDF':False}))
adj=pd.DataFrame([{'security':r['security'],'date':row['date'],'split_adjusted_price':row.get('PX_LAST')} for r in rawadj for row in r.get('fieldData',[])])
h=h.merge(adj,on=['security','date'],how='left')
h['return']=h.groupby('security')['split_adjusted_price'].pct_change(fill_method=None)
h['amihud_per_million']=h['return'].abs()/(h['TURNOVER']/1e6)
h.loc[(h['TURNOVER']<=0)|(~np.isfinite(h['amihud_per_million'])),'amihud_per_million']=np.nan
liquidity=[]
for sec,g in h.groupby('security'):
    valid=g[(g['PX_VOLUME']>0)&(g['TURNOVER']>0)]
    last20=valid.tail(20)
    liquidity.append({'security':sec,'history_days':len(valid),'adtv_usd':valid['TURNOVER'].mean(),'adv_shares':valid['PX_VOLUME'].mean(),'adtv20_usd':last20['TURNOVER'].mean(),'amihud_per_million':valid['amihud_per_million'].mean(),'amihud20_per_million':last20['amihud_per_million'].mean(),'history_last_date':g['date'].max(),'history_return_price_only':g['split_adjusted_price'].iloc[-1]/g['split_adjusted_price'].iloc[0]-1})
df=df.merge(pd.DataFrame(liquidity),on='security',how='left')
df['daily_float_turnover_pct']=df['adv_shares']/df['float_shares']*100
df['daily_shares_turnover_pct']=df['adv_shares']/df['shares_out']*100
df['daily_cap_turnover_pct']=df['adtv_usd']/df['market_cap_usd']*100
df['cap_billions']=df['market_cap_usd']/1e9
df['adtv_millions']=df['adtv_usd']/1e6
# Analysis population excludes S&P companies outside Russell, bad currencies and sparse histories.
pop=df[df['in_russell']&(df['CRNCY']=='USD')&(df['market_cap_usd']>0)&(df['history_days']>=40)&df['sector'].notna()].copy()
metrics=['cap_billions','adtv_millions','daily_float_turnover_pct','daily_shares_turnover_pct','amihud_per_million','forward_pe','ev_ebitda','price_book','RETURN_ON_ASSET','PROF_MARGIN','SALES_GROWTH','NET_DEBT_TO_EBITDA','float_fraction','spread_bps_snapshot']
def summaries(d):
    out={}
    for excluded,g in d.groupby('excluded'):
        out['excluded' if excluded else 'included']={'companies':len(g),'metrics':{m:{'median':float(g[m].replace([np.inf,-np.inf],np.nan).median()),'n':int(g[m].replace([np.inf,-np.inf],np.nan).notna().sum())} for m in metrics}}
    return out
# Avoid sector-specific extrapolation beyond the observed size range of either group.
common=[]
for sector,g in pop.groupby('sector'):
    a=g[g['excluded']==1]['market_cap_usd'];b=g[g['excluded']==0]['market_cap_usd']
    if len(a) and len(b):
        lo=max(a.min(),b.min());hi=min(a.max(),b.max())
        common.append(g[g['market_cap_usd'].between(lo,hi)])
support=pd.concat(common,ignore_index=True)
# HC3 sandwich standard errors, normal approximate 95% intervals, no scipy dependency.
def fit_model(data,outcome,quality=False,industry=False,leverage=False,future=False):
    d=data.copy()
    d=d[(d[outcome]>0)&np.isfinite(d[outcome])&(d['market_cap_usd']>0)]
    if outcome=='ev_ebitda':d=d[~d['GICS_SECTOR'].isin([40,60])]
    controls=['RETURN_ON_ASSET','SALES_GROWTH','BETA_RAW_OVERRIDABLE'] if quality else []
    if leverage:controls+=['NET_DEBT_TO_EBITDA']
    if future:controls+=['forward_sales_growth_pct']
    d=d.dropna(subset=controls+['GICS_INDUSTRY' if industry else 'GICS_SECTOR'])
    if len(d)<30 or d['excluded'].nunique()<2:return {'error':'insufficient data'}
    cols=[np.ones(len(d)),d['excluded'].to_numpy(float),np.log(d['market_cap_usd'].to_numpy(float))]
    # allow a nonlinear relation between size and outcome
    centered=cols[-1]-cols[-1].mean();cols.append(centered**2)
    for c in controls:
        x=d[c].astype(float);x=x.clip(x.quantile(.01),x.quantile(.99));cols.append(x.to_numpy())
    fe=pd.get_dummies(d['GICS_INDUSTRY' if industry else 'GICS_SECTOR'].astype(str),drop_first=True,dtype=float)
    for c in fe:cols.append(fe[c].to_numpy())
    X=np.column_stack(cols);y=np.log(d[outcome].to_numpy(float))
    inv=np.linalg.pinv(X.T@X);beta=np.linalg.lstsq(X,y,rcond=None)[0];res=y-X@beta
    hat=np.sum((X@inv)*X,axis=1);weights=(res/np.maximum(1-hat,1e-8))**2
    cov=inv@(X.T@(weights[:,None]*X))@inv;se=float(np.sqrt(max(cov[1,1],0)))
    coef=float(beta[1]);p=math.erfc(abs(coef/se)/math.sqrt(2)) if se else 0
    # predicted at each company's own controls, with exclusion dummy removed, for descriptive screening
    fitted=X@beta;d['benchmark_log']=fitted-beta[1]*d['excluded'];d['residual_vs_included_pct']=100*np.expm1(y-d['benchmark_log'])
    result={'n':len(d),'n_excluded':int(d['excluded'].sum()),'coef_log_excluded':coef,'excluded_difference_pct':100*math.expm1(coef),'ci95_pct':[100*math.expm1(coef-1.96*se),100*math.expm1(coef+1.96*se)],'p_normal_approx':p,'p_bonferroni6':min(1,p*6),'r_squared':1-float(res@res)/float((y-y.mean())@(y-y.mean()))}
    return result,d
models={};residuals={}
outcomes=['adtv_usd','daily_float_turnover_pct','amihud_per_million','forward_pe','ev_ebitda','price_book']
for outcome in outcomes:
    for name,quality,industry,lev in [('size_sector',False,False,False),('size_sector_quality',True,False,False),('size_industry_quality',True,True,False)]:
        fitted=fit_model(support,outcome,quality,industry,lev)
        if isinstance(fitted,tuple):
            result,fd=fitted;models[outcome+'__'+name]=result
            if name=='size_industry_quality':residuals[outcome]=fd[['company_id','benchmark_log','residual_vs_included_pct']]
        else:models[outcome+'__'+name]=fitted
    if outcome=='ev_ebitda':
        fitted=fit_model(support,outcome,True,True,True)
        if isinstance(fitted,tuple):models[outcome+'__size_industry_quality_leverage']=fitted[0]
future_models={}
for outcome in outcomes:
    fitted=fit_model(support,outcome,True,True,outcome=='ev_ebitda',True)
    future_models[outcome]=fitted[0] if isinstance(fitted,tuple) else fitted
twenty_day_models={}
for outcome in ['adtv20_usd','amihud20_per_million']:
    fitted=fit_model(support,outcome,True,True)
    twenty_day_models[outcome]=fitted[0] if isinstance(fitted,tuple) else fitted
# Greedy closest size matching with no replacement, exact industry, cap ratio <=2.
def make_pairs(data,sector_only=False):
    a=data[data['excluded']==1];b=data[data['excluded']==0];edges=[]
    group='GICS_SECTOR' if sector_only else 'GICS_INDUSTRY'
    for i,r in a.iterrows():
        candidates=b[b[group]==r[group]]
        for j,t in candidates.iterrows():
            distance=abs(math.log(r['market_cap_usd']/t['market_cap_usd']))
            if distance<=math.log(2):edges.append((distance,i,j))
    useda=set();usedb=set();pairs=[]
    for distance,i,j in sorted(edges):
        if i not in useda and j not in usedb:
            pairs.append((i,j,distance));useda.add(i);usedb.add(j)
    return pairs
pairs=make_pairs(pop)
rng=np.random.default_rng(20261005)
pair_summary={}
pair_rows=[]
for i,j,dist in pairs:
    a=pop.loc[i];b=pop.loc[j]
    row={'excluded_security':a['security'],'included_security':b['security'],'excluded_name':a['NAME'],'included_name':b['NAME'],'industry':a['GICS_INDUSTRY'],'cap_ratio':a['market_cap_usd']/b['market_cap_usd']}
    for m in metrics:
        row['excluded_'+m]=a[m];row['included_'+m]=b[m]
    pair_rows.append(row)
for m in metrics:
    a=np.array([pop.loc[i,m] for i,j,_ in pairs],float);b=np.array([pop.loc[j,m] for i,j,_ in pairs],float)
    good=np.isfinite(a)&np.isfinite(b)
    if m in ['ev_ebitda']:
        good &=np.array([pop.loc[i,'GICS_SECTOR'] not in [40,60] for i,j,_ in pairs])
    ag=a[good];bg=b[good]
    out={'n':len(ag),'excluded_median':float(np.median(ag)) if len(ag) else None,'included_median':float(np.median(bg)) if len(ag) else None,'median_paired_difference':float(np.median(ag-bg)) if len(ag) else None}
    pos=(ag>0)&(bg>0);lr=np.log(ag[pos]/bg[pos])
    if len(lr):
        boots=np.array([np.median(rng.choice(lr,len(lr),replace=True)) for _ in range(2000)])
        out.update({'positive_pairs':len(lr),'median_paired_ratio':float(np.exp(np.median(lr))),'ratio_ci95_bootstrap':np.exp(np.quantile(boots,[.025,.975])).tolist()})
    pair_summary[m]=out
# Descriptive residuals, not out-of-sample return forecasts.
screen=pop[pop['excluded']==1].copy()
for m in ['forward_pe','ev_ebitda','adtv_usd','amihud_per_million']:
    if m in residuals:
        screen=screen.merge(residuals[m][['company_id','residual_vs_included_pct']].rename(columns={'residual_vs_included_pct':m+'_residual_pct'}),on='company_id',how='left')
short=screen[(screen['forward_pe']>0)&(screen['RETURN_ON_ASSET']>0)&(screen['SALES_GROWTH']>=0)&(screen['forward_pe_residual_pct']<-20)&(screen['adtv_usd_residual_pct']<0)].copy()
# require a reasonably close same-industry control, and report it explicitly
shortrows=[]
for _,r in short.iterrows():
    peers=pop[(pop['excluded']==0)&(pop['GICS_INDUSTRY']==r['GICS_INDUSTRY'])&(pop['forward_pe']>0)].copy()
    peers['distance']=abs(np.log(peers['market_cap_usd']/r['market_cap_usd']))
    peers=peers[peers['distance']<=math.log(2)]
    if not len(peers):continue
    p=peers.sort_values('distance').iloc[0]
    shortrows.append({'security':r['security'],'name':r['NAME'],'sector':r['sector'],'cap_billions':r['cap_billions'],'forward_pe':r['forward_pe'],'ev_ebitda':r['ev_ebitda'],'roa_pct':r['RETURN_ON_ASSET'],'sales_growth_pct':r['SALES_GROWTH'],'net_debt_ebitda':r['NET_DEBT_TO_EBITDA'],'adtv_millions':r['adtv_millions'],'float_turnover_pct':r['daily_float_turnover_pct'],'amihud_per_million':r['amihud_per_million'],'forward_pe_residual_pct':r['forward_pe_residual_pct'],'adtv_residual_pct':r['adtv_usd_residual_pct'],'peer':p['security'],'peer_name':p['NAME'],'peer_cap_billions':p['cap_billions'],'peer_forward_pe':p['forward_pe'],'peer_ev_ebitda':p['ev_ebitda'],'peer_roa_pct':p['RETURN_ON_ASSET'],'peer_sales_growth_pct':p['SALES_GROWTH'],'peer_net_debt_ebitda':p['NET_DEBT_TO_EBITDA'],'peer_adtv_millions':p['adtv_millions']})
shortrows=sorted(shortrows,key=lambda x:x['forward_pe_residual_pct'])
sector_summary=[]
for sector,g in pop.groupby('sector'):
    row={'sector':sector}
    for e in [1,0]:
        z=g[g['excluded']==e];label='excluded' if e else 'included'
        row[label+'_n']=len(z)
        for m in ['cap_billions','adtv_millions','daily_float_turnover_pct','amihud_per_million','forward_pe','ev_ebitda']:row[label+'_'+m]=float(z[m].median()) if len(z) else None
    sector_summary.append(row)
# Regression robustness restricted to profitable and positive-growth firms.
robust={}
for m in outcomes:
    fitted=fit_model(support[(support['RETURN_ON_ASSET']>0)&(support['SALES_GROWTH']>=0)],m,True,True,m=='ev_ebitda')
    robust[m]=fitted[0] if isinstance(fitted,tuple) else fitted
summary={'retrieved_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'history_window':['2026-07-01','2026-10-02'],'index_update_dates':[x['fieldData']['LAST_UPDATE_DT'] for x in universe],
'forward_growth_robustness':future_models,'twenty_day_robustness':twenty_day_models,
'counts':{'excluded_above_22_7bn':int(((df['excluded']==1)&(df['cap_billions']>=22.7)).sum()),'russell_security_lines':len(russell),'sp_security_lines':len(sp),'russell_companies':int(df['in_russell'].sum()),'sp_companies':int(df['in_sp500'].sum()),'russell_not_sp_companies':int(df['excluded'].sum()),'russell_and_sp_companies':int((df['in_russell']&df['in_sp500']).sum()),'sp_outside_russell':df.loc[df['in_sp500']&~df['in_russell'],['security','NAME']].to_dict('records'),'company_ids_missing':int(dfsec['ID_BB_GLOBAL_COMPANY'].isna().sum()),'analysis_population':len(pop),'common_size_support':len(support),'exact_industry_size_pairs':len(pairs),'history_rows':len(h)},
'quality':{'security_errors':errors,'history_errors':[{'security':r.get('security'),'securityError':r.get('securityError'),'fieldExceptions':r.get('fieldExceptions')} for r in rawhist+rawadj if r.get('securityError') or r.get('fieldExceptions')],'hist_days_distribution':df['history_days'].describe().to_dict(),'currencies':df['CRNCY'].value_counts().to_dict(),'negative_or_crossed_spreads':int(((df['PX_BID']<=0)|(df['PX_ASK']<df['PX_BID'])).sum()),'latest_history_date_counts':df['history_last_date'].value_counts().to_dict(),'company_dedup_removed_security_lines':len(dfsec)-len(df),'excluded_from_analysis':df.loc[df['in_russell']&~df['company_id'].isin(pop['company_id']),['security','NAME','history_days','CRNCY']].to_dict('records')},
'raw_group_medians':summaries(pop),'models':models,'matched_pairs':pair_summary,'profitable_growth_robustness':robust,'sector_summary':sector_summary,'screen_count':len(shortrows),'screen':shortrows}
# Relation of valuation residual and liquidity residual conditional on membership and controls.
association={}
if 'forward_pe' in residuals:
    for metric in ['adtv_usd','amihud_per_million']:
        z=residuals['forward_pe'].merge(residuals[metric],on='company_id',suffixes=('_pe','_liquidity'))
        association[metric]={'n':len(z),'spearman_residual_correlation':float(z['residual_vs_included_pct_pe'].corr(z['residual_vs_included_pct_liquidity'],method='pearson')) if False else float(z['residual_vs_included_pct_pe'].rank().corr(z['residual_vs_included_pct_liquidity'].rank()))}
summary['valuation_liquidity_residual_association']=association
def clean(obj):
    if isinstance(obj,dict):return {str(k):clean(v) for k,v in obj.items()}
    if isinstance(obj,list):return [clean(v) for v in obj]
    if isinstance(obj,(np.integer,)):return int(obj)
    if isinstance(obj,(float,np.floating)):return float(obj) if math.isfinite(obj) else None
    return obj
summary=clean(summary)
if SAVE:
    ROOT.mkdir(exist_ok=True)
    dfsec.to_csv(ROOT/'security_membership_and_reference.csv',index=False)
    df.to_csv(ROOT/'company_metrics_all.csv',index=False)
    df[df['excluded']==1].to_csv(ROOT/'russell1000_not_sp500.csv',index=False)
    h.to_csv(ROOT/'daily_liquidity_20260701_20261002.csv',index=False)
    pd.DataFrame(pair_rows).to_csv(ROOT/'matched_company_pairs.csv',index=False)
    pd.DataFrame(shortrows).to_csv(ROOT/'descriptive_candidates.csv',index=False)
    pd.DataFrame(sector_summary).to_csv(ROOT/'sector_comparison.csv',index=False)
    (ROOT/'analysis_summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    (ROOT/'bloomberg_raw_responses.json').write_text(json.dumps({'universe':universe,'reference':rawref,'forecast2bf':rawforecast2,'history':rawhist,'split_adjusted_history':rawadj},default=str),encoding='utf-8')
    (ROOT/'methodology.txt').write_text('Snapshot queried from Bloomberg desktop API on 2026-10-05; index LAST_UPDATE_DT 2026-10-02 is not a constituent effective-date guarantee.\nUniverse: RIY Index, SPX Index; company identity: ID_BB_GLOBAL_COMPANY. Constituent exchange tickers normalized to US composite tickers for consolidated prices, share volume and traded values. One representative line per company, S&P lines preferred; otherwise most active Russell line.\nReference override SCALING_FORMAT=UNT; shares/float remain millions per Bloomberg field definitions and are multiplied by 1e6. BEST_FPERIOD_OVERRIDE=1BF (blended next 12 months). Current EV/EBITDA, not period-end EV. Fundamental ratios reflect latest available reporting periods, not identical fiscal dates.\nHistory: 2026-07-01 through 2026-10-02, daily actual USD traded values and unadjusted share volume; separately split-adjusted price changes, excluding dividend adjustments. At least 40 valid trading days.\nADTV=mean TURNOVER; float turnover=mean volume/current public float; Amihud=mean absolute split-adjusted close return divided by traded USD millions, a price-impact proxy rather than measured execution cost. Current float is used throughout, not point-in-time historical float.\nBaseline regression: log(metric)=exclusion + log(market cap) + centered log(cap)^2 + sector fixed effects. Quality models add ROA, revenue growth, beta (1/99-percentile winsorized). Industry model replaces sector with exact GICS industry fixed effects. HC3 standard errors, normal approximate confidence intervals. Separate sector-wise overlapping size support. EV/EBITDA excludes Financials and Real Estate; leverage robustness adds net debt/EBITDA. Forward-growth robustness adds implied sales growth from blended months 13-24 / months 1-12 consensus; 20-day robustness repeats dollar volume and Amihud models for last 20 trading days.\nMultiple testing adjustment Bonferroni6 applies to six outcomes within each model family, not all exploratory variants.\nMatching: exact industry, cap within 2x, greedy globally smallest logcap difference first, no replacement. Bootstrap 2000 draws of median log paired ratios with seed 20261005. Reported paired interval is exploratory/unadjusted for multiplicity.\nCandidate screen: positive forward P/E, positive ROA, nonnegative sales growth, P/E residual below -20%, negative ADTV residual, same-industry included peer within 2x cap. Residual is model-relative pricing, not fair value or expected return.\nCross-section cannot identify causal impact of S&P exclusion or prove arbitrage. No point-in-time membership backtest, inclusion-event causal study, borrowing costs, execution/slippage simulation, or forward alpha test was performed. Both groups already belong to Russell 1000; other index/ETF inclusion was not controlled.\n',encoding='utf-8')
    progress('SAVED '+str(ROOT))
# Keep console output compact; full results in saved JSON.
console={**summary,'quality':{**summary['quality'],'security_errors_count':len(errors),'security_errors':errors[:5]},'screen':shortrows[:12]}
progress('RESULT_JSON '+json.dumps(console,ensure_ascii=True,default=str))
s.stop()
