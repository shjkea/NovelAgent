(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { status:null, config:null, routing:null, pricing:null, lastPrompt:localStorage.getItem('na_dlc_last_prompt')||'', eventSource:null, promptLoaded:false, promptOriginal:'', mdBeforeHash:null, auditRepairLoadedSig:'', auditCompare:null };

  function fmtNum(v, d=1){ const n=Number(v||0); return Number.isFinite(n)?n.toFixed(d):'0'; }
  function fmtInt(v){ const n=Number(v||0); return Number.isFinite(n)?Math.round(n).toLocaleString('zh-CN'):'0'; }
  function fmtTime(sec){ sec=Math.max(0,Number(sec||0)); const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=Math.floor(sec%60); return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`; }
  function fmtIdle(v, running){ if(v===null||v===undefined) return running?'等待首 token':'—'; const x=Number(v); if(x<1)return `${x.toFixed(1)} 秒`; if(x<60)return `${x.toFixed(0)} 秒`; return fmtTime(x); }
  function esc(s){ return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
  function text(id, value){ const el=$(id); if(el) el.textContent = value ?? '—'; }
  function setPill(id, label, cls='neutral'){ const el=$(id); if(!el)return; el.className=`pill ${cls}`; el.textContent=label; }
  function setDot(id, ok, warn=false){ const el=$(id); if(!el)return; el.className='dot '+(warn?'warn':ok?'good':'bad'); }
  function toast(msg, kind=''){ const el=document.createElement('div'); el.className=`toast ${kind}`; el.textContent=msg; $('toastWrap').appendChild(el); setTimeout(()=>el.remove(),4200); }
  function setPreview(id, value, placeholder){ const el=$(id); if(!el)return; const atBottom=(el.scrollHeight-el.scrollTop-el.clientHeight)<42; const v=String(value||''); el.textContent=v||placeholder; if(atBottom)el.scrollTop=el.scrollHeight; }
  function appendPreview(id, chunk){ const el=$(id); if(!el||!chunk)return; const atBottom=(el.scrollHeight-el.scrollTop-el.clientHeight)<42; if(el.textContent==='尚未收到正文输出。'||el.textContent==='尚未收到 DLC 输出。')el.textContent=''; el.textContent=(el.textContent+chunk).slice(-80000); if(atBottom)el.scrollTop=el.scrollHeight; }
  function compareLineOps(original, candidate){
    const left=String(original??'').replace(/\r\n?/g,'\n').split('\n');
    const right=String(candidate??'').replace(/\r\n?/g,'\n').split('\n');
    let prefix=0;
    while(prefix<left.length&&prefix<right.length&&left[prefix]===right[prefix])prefix++;
    let suffix=0;
    while(suffix<left.length-prefix&&suffix<right.length-prefix&&left[left.length-1-suffix]===right[right.length-1-suffix])suffix++;
    const leftMid=left.slice(prefix,left.length-suffix), rightMid=right.slice(prefix,right.length-suffix);
    const ops=[];
    for(let i=0;i<prefix;i++)ops.push({type:'equal',left:left[i],right:right[i]});
    const product=leftMid.length*rightMid.length;
    if(product<=180000){
      const table=Array.from({length:leftMid.length+1},()=>new Uint16Array(rightMid.length+1));
      for(let i=leftMid.length-1;i>=0;i--){
        for(let j=rightMid.length-1;j>=0;j--)table[i][j]=leftMid[i]===rightMid[j]?table[i+1][j+1]+1:Math.max(table[i+1][j],table[i][j+1]);
      }
      let i=0,j=0;
      while(i<leftMid.length&&j<rightMid.length){
        if(leftMid[i]===rightMid[j]){ops.push({type:'equal',left:leftMid[i],right:rightMid[j]});i++;j++;}
        else if(table[i+1][j]>=table[i][j+1]){ops.push({type:'remove',left:leftMid[i++]});}
        else{ops.push({type:'add',right:rightMid[j++]});}
      }
      while(i<leftMid.length)ops.push({type:'remove',left:leftMid[i++]});
      while(j<rightMid.length)ops.push({type:'add',right:rightMid[j++]});
    }else{
      leftMid.forEach(line=>ops.push({type:'remove',left:line}));
      rightMid.forEach(line=>ops.push({type:'add',right:line}));
    }
    for(let i=0;i<suffix;i++)ops.push({type:'equal',left:left[left.length-suffix+i],right:right[right.length-suffix+i]});
    return ops;
  }
  function compareRows(ops){
    const rows=[]; let index=0;
    while(index<ops.length){
      const op=ops[index];
      if(op.type==='equal'){rows.push(op);index++;continue;}
      const removed=[], added=[];
      while(index<ops.length&&ops[index].type!=='equal'){
        if(ops[index].type==='remove')removed.push(ops[index].left);
        else if(ops[index].type==='add')added.push(ops[index].right);
        index++;
      }
      const count=Math.max(removed.length,added.length);
      for(let i=0;i<count;i++)rows.push({type:'change',left:i<removed.length?removed[i]:undefined,right:i<added.length?added[i]:undefined});
    }
    return rows;
  }
  function inlineDiffHtml(left,right,side){
    const value=String(side==='left'?left:right??'');
    if(side==='left'&&left===undefined||side==='right'&&right===undefined)return '&nbsp;';
    if(left===right)return esc(value)||'&nbsp;';
    const other=String(side==='left'?right??'':left??'');
    let prefix=0;
    while(prefix<value.length&&prefix<other.length&&value[prefix]===other[prefix])prefix++;
    let suffix=0;
    while(suffix<value.length-prefix&&suffix<other.length-prefix&&value[value.length-1-suffix]===other[other.length-1-suffix])suffix++;
    const end=value.length-suffix;
    const changed=value.slice(prefix,end);
    const markClass=side==='left'?'diff-char-remove':'diff-char-add';
    return `${esc(value.slice(0,prefix))}<mark class="${markClass}">${esc(changed)||'&nbsp;'}</mark>${esc(value.slice(end))}`||'&nbsp;';
  }
  function renderComparePayload(){
    const payload=state.auditCompare;
    if(!payload)return;
    const ops=compareLineOps(payload.original,payload.candidate), rows=compareRows(ops), only=!!$('chapterCompareChangedOnly')?.checked;
    let leftNo=1,rightNo=1;
    const leftHtml=[],rightHtml=[];
    rows.forEach(row=>{
      const changed=row.type!=='equal';
      if(only&&!changed){leftNo++;rightNo++;return;}
      const leftClass=changed?(row.left===undefined?'diff-line diff-empty':'diff-line diff-remove'):'diff-line';
      const rightClass=changed?(row.right===undefined?'diff-line diff-empty':'diff-line diff-add'):'diff-line';
      const changeClass=changed?' diff-change':'';
      const leftAlias=changed&&row.left!==undefined?' diff-del':'';
      const rightAlias=changed&&row.right!==undefined?' diff-add':'';
      leftHtml.push(`<span class="${leftClass}${changeClass}${leftAlias}"><span class="diff-ln">${row.left===undefined?'':leftNo}</span><span class="diff-text">${inlineDiffHtml(row.left,row.right,'left')}</span></span>`);
      rightHtml.push(`<span class="${rightClass}${changeClass}${rightAlias}"><span class="diff-ln">${row.right===undefined?'':rightNo}</span><span class="diff-text">${inlineDiffHtml(row.left,row.right,'right')}</span></span>`);
      if(row.left!==undefined)leftNo++;
      if(row.right!==undefined)rightNo++;
    });
    $('chapterCompareOriginal').innerHTML=leftHtml.join('')||'<span class="diff-empty-state">（无内容）</span>';
    $('chapterCompareCandidate').innerHTML=rightHtml.join('')||'<span class="diff-empty-state">（无内容）</span>';
    const force=$('chapterCompareForceBtn');
    if(force){force.disabled=!payload.forceAvailable;force.hidden=!payload.isAudit;force.textContent='强制提交本章';force.dataset.chapterNo=String(payload.chapterNo||'');}
  }
  async function api(url, opt={}){
    const r=await fetch(url,{credentials:'same-origin',...opt});
    const j=await r.json().catch(()=>({detail:r.statusText||`HTTP ${r.status}`}));
    if(r.status===401){ location.reload(); throw new Error('未登录'); }
    if(!r.ok){ const e=new Error(typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||j)); e.status=r.status; e.payload=j; throw e; }
    return j;
  }
  function post(url, body){ return api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)}); }

  function updateCanon(s){
    const running=!!s.running;
    const highContext=!!s.high_context_mode_enabled;
    const external=s.external_canon||{};
    const overflow=s.plan_overflow||{};
    const overflowPending=!!overflow.pending;
    text('canonChapter', s.chapter?`第 ${s.chapter} 章`:'—');
    text('canonStage', s.stage||'空闲');
    const apiProvider=(s.stage_provider==='deepseek'?(s.stage_api_source==='volcengine_agent_plan'?'火山 Agent Plan':'DeepSeek 官方'):s.stage_provider); text('canonProvider', apiProvider|| (running?'—':'DeepSeek Canon'));
    text('canonModel', s.stage_model||'—');
    text('canonTps', fmtNum(s.display_tps,1));
    text('canonPromptTps', fmtNum(s.prompt_tps,1));
    text('canonIdle', fmtIdle(s.stream_idle_seconds,running));
    text('canonElapsed', fmtTime(s.stage_elapsed_seconds));
    text('canonChars', fmtInt(s.chapter_chars));
    text('canonCompletionTokens', fmtInt(s.stage_completion_tokens||s.completion_tokens_last));
    text('canonReasoningTokens', fmtInt(s.stage_reasoning_tokens));
    text('canonComplexity', `${s.chapter_complexity_label||'普通'} ${fmtInt(s.chapter_complexity_score||0)}/10`);
    text('canonContextTarget', s.stage_context_target_tokens?`${fmtInt(s.stage_context_target_tokens)} tok`:'—');
    if($('highContextToggle')){$('highContextToggle').checked=highContext;$('highContextToggle').disabled=running;}
    text('highContextLimitLabel',`${fmtInt(s.high_context_target_tokens||120000)} / ${fmtInt(s.high_context_max_tokens||127000)}`);
    if(s.stage_api_source==='volcengine_agent_plan'){ const afp=s.stage_afp_estimate; text('canonBilling',afp===null||afp===undefined?'本阶段 AFP：输入>32K，暂不估算':`本阶段 AFP ≈ ${fmtNum(afp,3)}`); } else text('canonBilling',`本阶段 ¥${fmtNum(s.stage_cost_cny,4)}`);
    const pct=Math.max(0,Math.min(100,Number(s.stage_context_pct||0)));
    $('contextBar').style.width=`${pct}%`;
    text('contextLabel', `${fmtInt(s.stage_prompt_tokens)} / ${fmtInt(s.stage_context_limit)} · ${pct.toFixed(1)}%`);
    $('liveToggle').checked=!!s.live_output;
    if($('canonPreviewBlock')) $('canonPreviewBlock').hidden=!s.live_output;
    if(!s.live_output && $('canonPreview')) $('canonPreview').textContent='尚未收到正文输出。';
    if(running){
      if(s.stream_stalled) setPill('canonRunPill','疑似停滞','bad'); else if(Number(s.stream_idle_seconds||0)>8) setPill('canonRunPill','等待输出','warn'); else setPill('canonRunPill','运行中','good');
    }else if(overflowPending) setPill('canonRunPill','等待超限确认','warn');
    else setPill('canonRunPill','空闲','neutral');
    const idleHint=$('canonIdleHint');
    if(idleHint) idleHint.textContent=s.display_tps_source||'stream';
    $('canonStartBtn').disabled=running||overflowPending||!!external.running||!!external.generation_blocked;
    $('canonStopBtn').disabled=!running;
    const overflowPanel=$('planOverflowPanel');
    if(overflowPanel) overflowPanel.hidden=!overflowPending;
    if(overflowPending){
      const hardBlocked=!!overflow.hard_blocked;
      const costGuard=String(overflow.reason||'').includes('cost_guard');
      const unit=overflow.cost_guard_mode==='cny'?'元':' AFP';
      text('planOverflowTitle',hardBlocked?`第 ${overflow.chapter||'—'} 章超过 Plan 上下文上限`:costGuard?`第 ${overflow.chapter||'—'} 章等待费用确认`:`第 ${overflow.chapter||'—'} 章 Plan 上下文超限`);
      text('planOverflowDetail',hardBlocked?`估算 ${fmtInt(overflow.estimated_tokens)} tokens，上限 ${fmtInt(overflow.safe_tokens)}。本次请求未发送，不能手动绕过。`:costGuard?`前 ${overflow.auto_window_size||10} 个已完成章节中，已有 ${overflow.auto_window_used||0} 章的整章实际总费用超过 ${fmtNum(overflow.cost_guard_limit,overflow.cost_guard_mode==='cny'?2:1)}${unit}；本章尚未发送任何生成请求。`:`本次规划请求尚未发送。`);
      if($('planOverflowContinueBtn')){$('planOverflowContinueBtn').hidden=hardBlocked;$('planOverflowContinueBtn').disabled=running||hardBlocked;}
      if($('planOverflowCancelBtn')) $('planOverflowCancelBtn').disabled=running;
    }else if($('planOverflowContinueBtn')){
      $('planOverflowContinueBtn').hidden=false;
    }
    if(external.generation_blocked) $('canonNotice').textContent=external.gate_message||'外部正史范围尚未完整导入，后续 Canon 生成已锁定。';
    else if(overflowPending) $('canonNotice').textContent='Plan 预检已安全停止，等待你的选择；当前没有发送规划请求。';
    else if(s.last_error) $('canonNotice').textContent=`最近错误：${s.last_error}`;
    else if(s.handoff_status==='degraded'||s.handoff_status==='error') $('canonNotice').textContent=`连续性交接降级：${s.handoff_error||'结构化 handoff 缺失'}；已保留 ${fmtInt(s.handoff_tail_chars)} 字真实正文末尾，批量生成不会静默继续。`;
    else $('canonNotice').textContent=`连续性交接：${s.handoff_status==='complete'?'完整':s.handoff_status==='first_chapter'?'第1章无上一章':'待生成'} · 上一章正文末尾 ${fmtInt(s.handoff_tail_chars)} 字；当前 API：${s.deepseek_source==='volcengine_agent_plan'?'火山 Agent Plan':'DeepSeek 官方'} / ${s.deepseek_account_label||'API 1'}。`;
    if(s.live_output && s.preview_text!==undefined) setPreview('canonPreview',s.preview_text,'尚未收到正文输出。');
    text('canonPreviewLabel',s.preview_label?`${s.preview_label} · 第 ${s.preview_chapter||s.chapter||'—'} 章`:'等待 Draft / Revision');
  }

  function updateDLC(d){
    d=d||{}; const running=!!d.running;
    text('dlcTps',fmtNum(d.display_tps,1)); text('dlcPromptTps',fmtNum(d.prompt_tps,1));
    text('dlcIdle',fmtIdle(d.stream_idle_seconds,running)); text('dlcChars',fmtInt(d.output_chars));
    if(running){
      if(d.stream_stalled)setPill('dlcRunPill',`第${d.chapter}章 · 停滞`,'bad');
      else if(Number(d.stream_idle_seconds||0)>8)setPill('dlcRunPill',`第${d.chapter}章 · 等待`,'warn');
      else setPill('dlcRunPill',`第${d.chapter}章 · 运行中`,'good');
    }else setPill('dlcRunPill','空闲','neutral');
    $('dlcGenerateBtn').disabled=running; $('dlcStopBtn').disabled=!running;
    if(running && d.chapter) $('dlcChapter').value=d.chapter;
    if(d.last_error) $('dlcNotice').textContent=`DLC 最近错误：${d.last_error}`;
    else if(running) $('dlcNotice').textContent=`${d.stage||'Grok 扩写'} · 抽样 ${d.current_draw||0}/${d.draw_count||1} · 通过 ${d.candidates_passed||0} · 拦截 ${d.candidates_blocked||0} · 请求 ${d.request_count||0} · $${fmtNum(d.cost_usd,4)}。`;
    else if(d.output_file) $('dlcNotice').textContent=`最近候选：${d.output_file} · 通过 ${d.candidates_passed||0} · 拦截 ${d.candidates_blocked||0} · 本批 $${fmtNum(d.cost_usd,4)}。`;
    else $('dlcNotice').textContent='Grok 直接读取完整指定章节、扩写提示词、相关图鉴和相关人物；不再生成 JSON 场景合同。';
    if(d.preview_text!==undefined) setPreview('dlcPreview',d.preview_text,'尚未收到 DLC 输出。');
    text('dlcPreviewLabel',d.running?`第 ${d.chapter||'—'} 章 · ${d.scene_id||'—'} · ${d.candidate_id||'候选准备中'} · ${d.current_draw||0}/${d.draw_count||1}`:(d.last_candidate_id?`最近 ${d.last_candidate_id}`:'等待 Grok'));
  }

  function updateReaderReflow(r){
    r=r||{}; const running=!!r.running, total=Number(r.item_total||0), done=Number(r.item_done||0);
    const pct=total?Math.max(0,Math.min(100,done*100/total)):Number(r.progress_pct||0);
    if($('readerReflowProgressBar'))$('readerReflowProgressBar').style.width=`${pct}%`;
    text('readerReflowProgressPct',`${pct.toFixed(1)}%`);
    text('readerReflowProgressLabel',running?`${r.stage_label||r.stage||'处理中'} · 第 ${r.chapter||'—'} 章 · ${done}/${total}`:(r.stage==='完成'?`批次完成 · ${done}/${total}`:r.stage||'等待任务'));
    text('readerReflowCompleted',fmtInt(r.completed)); text('readerReflowSkipped',fmtInt(r.skipped)); text('readerReflowFailed',fmtInt(r.failed));
    text('readerReflowRequests',fmtInt(r.request_count)); text('readerReflowAfp',fmtNum(r.afp,3)); text('readerReflowElapsed',fmtTime(r.elapsed_seconds));
    if(running)setPill('readerReflowPill',`${done}/${total} · 运行中`,'good');
    else if(Number(r.failed||0)>0)setPill('readerReflowPill',`完成 · ${r.failed} 章失败`,'warn');
    else if(r.stage==='完成')setPill('readerReflowPill','批次完成','good');
    else setPill('readerReflowPill','空闲','neutral');
    if($('readerReflowStartBtn'))$('readerReflowStartBtn').disabled=running;
    if($('readerReflowStopBtn'))$('readerReflowStopBtn').disabled=!running;
    if(running)text('readerReflowNotice',`DeepSeek V4 Flash（无 Thinking）正在处理；${r.workers||1} 并发。模型只返回边界，正文由本地逐字重组。`);
    else if(r.last_error)text('readerReflowNotice',`最近错误：${r.last_error}。其他校验通过的章节已经独立保存。`);
    else if(r.completed)text('readerReflowNotice',`已生成 ${r.completed} 章读者版；逐字校验全部通过。原 Canon 未修改。`);
  }

  function serviceState(s){ return `${s?.state||s?.status||'—'}${s?.detail?` · ${s.detail}`:''}`; }
  function updateServices(s){
    const e=s.embedding_control||{}; const ds=s.services?.deepseek||{};
    text('embedState',serviceState(e)); const src=s.deepseek_source==='volcengine_agent_plan'?'火山 Agent Plan':'DeepSeek 官方'; const acct=s.deepseek_account_label||'API 1'; text('deepseekState',s.deepseek_configured?`${src} · ${acct} · ${ds.detail||'已配置'}`:`${src} · ${acct} · 未配置 API Key`);
    setDot('embedDot',e.state==='ready'||e.ready===true, ['starting','loading','stopping'].includes(e.state));
    setDot('deepseekDot',!!ds.ok && !!s.deepseek_configured,!!s.deepseek_configured&&!ds.ok);
  }

  function updatePricing(p){
    if(!p)return; state.pricing=p;
    if(p.source==='volcengine_agent_plan'){
      setPill('pricePill','火山 · Agent Plan','good');
      setPill('priceDetailPill','AFP 套餐','good');
      text('priceNotice','当前使用火山方舟 Agent Plan：按 AFP 套餐额度抵扣，不使用 DeepSeek 官方峰/谷价；启动 Canon 不弹峰时提醒。');
      return;
    }
    const peak=!!p.peak; const sec=Number(p.seconds_to_switch||0);
    setPill('pricePill',`DeepSeek · ${peak?'峰时':'谷时'}`,peak?'warn':'good');
    setPill('priceDetailPill',peak?'峰价 ×2':'谷价',peak?'warn':'good');
    const next=p.next_switch_at?new Date(p.next_switch_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}):'—';
    text('priceNotice',`${peak?'当前峰时：API 价格约为谷时 2 倍。':'当前谷时：适合批量生成 Canon。'} 下次价格切换 ${next}（约 ${fmtTime(sec)} 后）。峰时 09:00–12:00、14:00–18:00。`);
  }
  async function startCanon(){
    try{
      const p=await api('/api/deepseek/pricing-status'); updatePricing(p);
      let confirmed=false;
      if(p.source==='official' && p.peak){ confirmed=confirm(`⚠ 当前处于 DeepSeek API 峰时\n\n峰时价格约为谷时的 2 倍。\n下次切换：${new Date(p.next_switch_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}\n\n仍然启动 Canon？`); if(!confirmed)return; }
      await post(`/api/start?confirm_peak=${confirmed?'true':'false'}`); toast('Canon 已启动','good'); await refreshStatus();
    }catch(e){toast(e.message,'bad');}
  }

  async function setHighContextMode(){
    const toggle=$('highContextToggle');
    const enabled=!!toggle.checked;
    toggle.disabled=true;
    try{
      const r=await post('/api/high-context',{enabled});
      toast(r.message||`高上下文模式已${enabled?'开启':'关闭'}`,'good');
      await refreshStatus();
    }catch(e){
      toggle.checked=!!state.status?.high_context_mode_enabled;
      toggle.disabled=!!state.status?.running;
      toast(e.message,'bad');
    }
  }

  async function continuePlanOverflow(){
    const p=state.status?.plan_overflow||{};
    if(!p.pending)return toast('当前没有等待确认的 Plan 超限请求','bad');
    const ok=confirm(`⚠ 第 ${p.chapter||'—'} 章 Plan 上下文超过安全线\n\n估算：${fmtInt(p.estimated_tokens)} tokens\n安全线：${fmtInt(p.safe_tokens)} tokens\n超出：${fmtInt(p.over_tokens)} tokens\n\n继续可能进入更高 AFP 档。本次确认只放行当前章这一次，仍然继续？`);
    if(!ok)return;
    try{
      const r=await post('/api/plan-overflow/continue');
      toast(r.message||'已一次性放行当前章','good');
      await refreshStatus();
    }catch(e){toast(e.message,'bad');}
  }

  async function cancelPlanOverflow(){
    try{
      const r=await post('/api/plan-overflow/cancel');
      toast(r.message||'已保持停止','good');
      await refreshStatus();
    }catch(e){toast(e.message,'bad');}
  }

  async function loadDeepSeekConfig(){
    try{
      const c=await api('/api/deepseek/config');
      $('deepseekSource').value=c.source||'official';

      const official=c.providers?.official||{};
      const volc=c.providers?.volcengine_agent_plan||{};

      const fillAccounts=(id,p)=>{
        const accounts=Array.isArray(p.accounts)?p.accounts:[];
        $(id).innerHTML=accounts.map(a=>`<option value="${esc(a.slot)}">${esc(a.label)}${a.configured?' · 已配置':' · 未配置'}</option>`).join('');
        $(id).value=String(p.active_slot||'1');
      };
      fillAccounts('deepseekOfficialAccount',official);
      fillAccounts('volcAccount',volc);

      const active=c.providers?.[c.source]||{};
      text('deepseekProviderNotice',`Canon 当前：${c.source==='volcengine_agent_plan'?'火山方舟 Agent Plan':'DeepSeek 官方'} / API ${active.active_slot||1}。这里只决定 Canon 使用平台。`);
    }catch(e){
      text('deepseekProviderNotice','API 配置读取失败：'+e.message);
    }
  }
  async function switchDeepSeekProvider(){
    const source=$('deepseekSource').value;
    try{
      const r=await post('/api/deepseek/provider',{source});
      toast(`已切换到 ${r.provider?.label||source}`,'good');
      await Promise.all([loadDeepSeekConfig(),refreshStatus(),refreshProviderStatuses()]);
      const p=await api('/api/deepseek/pricing-status'); updatePricing(p);
    }catch(e){ toast('切换失败：'+e.message,'bad'); await loadDeepSeekConfig(); }
  }

  async function switchPlatformAccount(source, selectId){
    const slot=$(selectId).value;
    try{
      const r=await post('/api/deepseek/account',{slot,source});
      toast(`已选择 ${source==='official'?'DeepSeek':'火山'} · ${r.account_label||`API ${slot}`}`,'good');
      await Promise.all([loadDeepSeekConfig(),refreshStatus()]);
      if(source==='official')await refreshDeepSeekBalance();
      else await refreshVolcAFP();
    }catch(e){
      toast('账号切换失败：'+e.message,'bad');
      await loadDeepSeekConfig();
    }
  }

  function moneySymbol(currency){ return currency==='CNY'?'¥':currency==='USD'?'$':`${currency||''} `; }
  function moneyText(v,currency){ const n=Number(v); return Number.isFinite(n)?`${moneySymbol(currency)}${n.toFixed(2)}`:'—'; }
  function smartNum(v){
    const n=Number(v);
    if(!Number.isFinite(n))return '0';
    if(Math.abs(n-Math.round(n))<1e-9)return Math.round(n).toLocaleString('zh-CN');
    return n.toFixed(2).replace(/0+$/,'').replace(/\.$/,'');
  }
  function resetText(ms){
    const t=Number(ms||0);
    if(!t)return '未提供重置时间';
    const sec=Math.max(0,Math.floor((t-Date.now())/1000));
    if(sec<=0)return '已到重置时间';
    const d=Math.floor(sec/86400), h=Math.floor((sec%86400)/3600), m=Math.floor((sec%3600)/60);
    if(d>0)return `${d}天${h}小时后重置`;
    if(h>0)return `${h}小时${m}分后重置`;
    return `${Math.max(1,m)}分钟后重置`;
  }
  function clearAFP(){
    [['afp5Usage','afp5Pct','afp5Reset','afp5Bar'],['afp7Usage','afp7Pct','afp7Reset','afp7Bar'],['afp30Usage','afp30Pct','afp30Reset','afp30Bar']].forEach(([u,p,r,b])=>{
      text(u,'—');text(p,'—');text(r,'—');if($(b))$(b).style.width='0%';
    });
  }
  function renderAFP(prefix,row){
    row=row||{};
    const used=Number(row.Used||0), quota=Number(row.Quota||0);
    const pct=quota>0?Math.max(0,Math.min(100,used/quota*100)):0;
    text(prefix+'Usage',`${smartNum(used)} / ${smartNum(quota)} AFP`);
    text(prefix+'Pct',`${pct.toFixed(1)}%`);
    text(prefix+'Reset',resetText(row.ResetTime));
    if($(prefix+'Bar'))$(prefix+'Bar').style.width=`${pct}%`;
  }

  async function refreshDeepSeekBalance(){
    setPill('deepseekBalancePill','查询中','neutral');
    try{
      const j=await api('/api/deepseek/balance');
      const label=j.account_label||`API ${j.slot||1}`;
      if(!j.configured){
        setPill('deepseekBalancePill','未配置','warn');
        text('deepseekOfficialState',`${label} · 未配置 API Key`);
        text('deepseekBalanceTotal','—');text('deepseekBalanceTopup','—');text('deepseekBalanceGrant','—');
        return;
      }
      if(!j.ok){
        setPill('deepseekBalancePill','查询失败','bad');
        text('deepseekOfficialState',`${label} · ${j.error||'余额查询失败'}`);
        return;
      }
      const infos=Array.isArray(j.balance_infos)?j.balance_infos:[];
      const b=infos.find(x=>x.currency==='CNY')||infos[0]||{};
      const c=b.currency||'CNY';
      text('deepseekBalanceTotal',moneyText(b.total_balance,c));
      text('deepseekBalanceTopup',moneyText(b.topped_up_balance,c));
      text('deepseekBalanceGrant',moneyText(b.granted_balance,c));
      text('deepseekOfficialState',`${label} · ${j.is_available?'账户可用':'余额不足或账户不可用'}`);
      setPill('deepseekBalancePill',j.is_available?'可用':'不可用',j.is_available?'good':'warn');
    }catch(e){
      setPill('deepseekBalancePill','查询失败','bad');
      text('deepseekOfficialState','余额查询失败：'+e.message);
    }
  }

  async function refreshVolcAFP(){
    setPill('volcAfpPill','查询中','neutral');
    try{
      const j=await api('/api/volcengine/afp');
      const label=j.account_label||`API ${j.slot||1}`;
      if(!j.openapi_configured){
        setPill('volcAfpPill','需 AK/SK','warn');
        text('volcState',`${label} · ${j.configured?'Agent Plan Key 已配置':'Agent Plan Key 未配置'} · ${j.error||'AFP 查询需 AK/SK'}`);
        clearAFP();
        return;
      }
      if(!j.ok){
        setPill('volcAfpPill','查询失败','bad');
        const rawErr=String(j.error||'AFP 查询失败');
        const hint=rawErr.includes('HTTP 401')
          ? `${rawErr} · 控制面 AK/SK 鉴权失败`
          : rawErr;
        text('volcState',`${label} · ${hint}`);
        clearAFP();
        return;
      }
      const u=j.usage||{};
      renderAFP('afp5',u.five_hour);
      renderAFP('afp7',u.weekly);
      renderAFP('afp30',u.monthly);
      text('volcState',`${label} · ${j.plan_type?`套餐 ${j.plan_type} · `:''}${j.configured?'Agent Plan Key 已配置':'Agent Plan Key 未配置'} · AFP 已刷新`);
      setPill('volcAfpPill','AFP 正常','good');
    }catch(e){
      setPill('volcAfpPill','查询失败','bad');
      text('volcState','AFP 查询失败：'+e.message);
      clearAFP();
    }
  }

  async function refreshProviderStatuses(){
    await Promise.all([refreshDeepSeekBalance(),refreshVolcAFP()]);
  }

  async function savePlatformKey(source,inputId,selectId){
    const api_key=$(inputId).value.trim();
    if(!api_key)return toast('请输入 API Key','bad');
    const slot=$(selectId).value;
    try{
      await post('/api/deepseek/key',{api_key,source,slot});
      $(inputId).value='';
      toast(`${source==='official'?'DeepSeek':'火山'} · API ${slot} Key 已保存`,'good');
      await loadDeepSeekConfig();
      if(source==='official')await refreshDeepSeekBalance();
      else await refreshVolcAFP();
    }catch(e){toast('保存失败：'+e.message,'bad');}
  }

  async function saveVolcOpenApiKeys(){
    const access_key=$('volcAccessKey').value.trim(), secret_key=$('volcSecretKey').value.trim();
    if(!access_key||!secret_key)return toast('AK 和 SK 都需要填写','bad');
    const slot=$('volcAccount').value;
    try{
      await post('/api/volcengine/openapi-key',{access_key,secret_key,slot});
      $('volcAccessKey').value='';$('volcSecretKey').value='';
      toast(`火山 API ${slot} 的 AK/SK 已保存`,'good');
      await refreshVolcAFP();
    }catch(e){toast('保存 AK/SK 失败：'+e.message,'bad');}
  }

  function billingText(b){
    b=b||{}; const mode=b.mode||'none';
    const cny=Number(b.cny||0), afp=Number(b.afp||0);
    if(mode==='mixed')return `¥${cny.toFixed(4)} + ${afp.toFixed(3)} AFP`;
    if(mode==='cny')return `¥${cny.toFixed(4)}`;
    if(mode==='afp')return `${afp.toFixed(3)} AFP`;
    return '点击查看';
  }

  function renderRecent(rows){
    const el=$('recentChapters'); const arr=Array.isArray(rows)?rows:[];
    if(!arr.length){el.innerHTML='<div class="notice">暂无章节记录</div>';return;}
    el.innerHTML=arr.map(r=>{ const n=r.chapter_no??r.chapter??r.id??'?'; const chars=r.chars??r.final_chars??''; const source=r.source==='external_canon'?'外部正史 · ':''; return `<div class="chapter-item" data-chapter="${esc(n)}"><b>第 ${esc(n)} 章</b><small>${esc(source)}${chars?`${esc(chars)} 字 · `:''}${esc(billingText(r.billing))}</small></div>`; }).join('');
    el.querySelectorAll('[data-chapter]').forEach(x=>x.onclick=()=>viewChapter(Number(x.dataset.chapter)));
  }

  function updateAudit(a){
    a=a||{}; const running=!!a.running;
    if(running){
      const seg=Number(a.segment_total||0)?`${a.segment_index||0}/${a.segment_total}`:'';
      setPill('auditPill',seg?`运行中 ${seg}`:'运行中','good');
      text('auditNotice',`${a.stage_label||a.stage||'审计中'} · 输入 ${fmtInt(a.prompt_tokens)} · 输出 ${fmtInt(a.completion_tokens)} · 缓存 ${fmtInt(a.cache_hit_tokens)} · ${Number(a.afp||0)>0?`${fmtNum(a.afp,3)} AFP`:`¥${fmtNum(a.cost_cny,4)}`}`);
    }else if(a.last_error){
      setPill('auditPill','失败','bad'); text('auditNotice',`最近审计失败：${a.last_error}`);
    }else if(a.report_file){
      const cls=a.status==='RED'?'bad':a.status==='ORANGE'||a.status==='YELLOW'?'warn':'good';
      setPill('auditPill',a.status||'完成',cls);
      text('auditNotice',`最近报告：${a.report_file} · ${Number(a.afp||0)>0?`${fmtNum(a.afp,3)} AFP`:`¥${fmtNum(a.cost_cny,4)}`}`);
    }else{
      setPill('auditPill','空闲','neutral');
      text('auditNotice','Flash 负责高召回候选；Pro 独立扫描全部正文窗口，并用双边逐字证据确认跨章硬错误。');
    }
    $('auditStartBtn').disabled=running||!!state.status?.running;
    $('auditStopBtn').disabled=!running;
    ['auditStart','auditEnd','auditSegmentSize','auditSourceCheck'].forEach(id=>{if($(id))$(id).disabled=running;});
    if(running && $('canonStartBtn')) $('canonStartBtn').disabled=true;
  }

  async function startAudit(){
    const start=Math.round(Number($('auditStart').value||0)), end=Math.round(Number($('auditEnd').value||0));
    const segment_size=Math.round(Number($('auditSegmentSize').value||4));
    if(start<1||end<start)return toast('审计章节范围无效','bad');
    if(segment_size<3||segment_size>12)return toast('全文窗口必须在 3-12 章之间','bad');
    try{
      await post('/api/audit/start',{start,end,segment_size,source_check:$('auditSourceCheck').checked});
      toast(`已开始审计第 ${start}-${end} 章`,'good'); await refreshStatus();
    }catch(e){toast('启动审计失败：'+e.message,'bad');}
  }

  async function viewAuditReport(){
    try{
      const j=await api('/api/audit/report'); $('viewerTitle').textContent=j.name||'剧情一致性审计'; $('viewerBody').textContent=j.content||''; $('viewerDialog').showModal();
    }catch(e){toast('读取审计报告失败：'+e.message,'bad');}
  }

  function updateAuditRepair(r){
    r=r||{}; const running=!!r.running;
    if(running){
      setPill('auditRepairPill','运行中','good');
      text('auditRepairNotice',`${r.stage_label||r.stage||'候选处理中'} · ${r.item_index||0}/${r.item_total||0} · 通过 ${r.candidate_ready||0} · 阻止 ${r.candidate_blocked||0} · ${Number(r.afp||0)>0?`${fmtNum(r.afp,3)} AFP`:`¥${fmtNum(r.cost_cny,4)}`}`);
    }else if(r.last_error){
      setPill('auditRepairPill','失败','bad');
      text('auditRepairNotice',r.last_error);
    }else if(r.batch_id){
      const cls=r.committed?'good':r.joint_safe===false?'warn':'neutral';
      setPill('auditRepairPill',r.committed?'已提交':r.rolled_back?'已回滚':r.stage||'已有批次',cls);
      text('auditRepairNotice',`${r.stage_label||`批次 ${r.batch_id}`} · ${Number(r.afp||0)>0?`${fmtNum(r.afp,3)} AFP`:`¥${fmtNum(r.cost_cny,4)}`}`);
    }else{
      setPill('auditRepairPill','空闲','neutral');
    }
    $('auditRepairPlanBtn').disabled=running;
    $('auditRepairRunBtn').disabled=running;
    $('auditRepairStopBtn').disabled=!running;
    if(running && $('canonStartBtn'))$('canonStartBtn').disabled=true;

    if(!running && r.batch_id){
      const sig=`${r.batch_id}|${r.stage||''}|${r.committed?'1':'0'}|${r.rolled_back?'1':'0'}`;
      if(state.auditRepairLoadedSig!==sig){
        state.auditRepairLoadedSig=sig;
        setTimeout(()=>loadAuditRepairBatch(true),0);
      }
    }
  }

  function auditRepairClassLabel(x){
    return ({TEXT_ONLY:'文字修正',CONTINUITY_MINOR:'连续性小修',REWRITE_SPAN:'结构性段落重写',REWRITE_CHAPTER:'整章定向重写',NEEDS_EVIDENCE:'等待证据定位',MANUAL_ONLY:'章节文件不可用',DEFER_FUTURE:'后续自然补足'})[x]||x||'—';
  }

  function auditRepairAttemptText(meta){
    const total=Math.max(1,Number(meta?.attempts||1));
    const semantic=Math.max(0,Number(meta?.semantic_attempts||0));
    const mechanical=Math.max(0,Number(meta?.mechanical_retries||0));
    const detail=[];
    if(semantic)detail.push(`语义重试 ${semantic}`);
    if(mechanical)detail.push(`机械重试 ${mechanical}`);
    return `自动尝试 ${total} 次${detail.length?`（${detail.join('，')}）`:''}`;
  }

  async function singleProRetry(batch_id, chapter_no, mode='retry'){
    const bid=String(batch_id||'').trim();
    const n=Number(chapter_no||0);
    const repairMode=mode==='deep'?'deep':'retry';
    const label=repairMode==='deep'?'Pro 深度修复':'Pro 重新生成';
    if(!bid)return toast('缺少修复批次，请先刷新修复计划','bad');
    if(!Number.isInteger(n)||n<=0)return toast('章节号错误','bad');

    const buttons=Array.from(document.querySelectorAll('[data-pro-retry]'));
    buttons.forEach(btn=>{btn.disabled=true;});
    try{
      const j=await post('/api/audit/repair/single_pro',{
        batch_id:bid,
        chapter_no:n,
        mode:repairMode,
      });
      toast(j.message||`已启动第 ${n} 章${label}`,'good');
      updateAuditRepair(j.status||{});
      await refreshStatus();
    }catch(e){
      toast(`${label}失败：${e.message}`,'bad');
      buttons.forEach(btn=>{btn.disabled=false;});
    }
  }

  async function loadAuditRepairBatch(silent=false){
    try{
      const j=await api('/api/audit/repair/batch');
      window.auditRepairBatchId=j.batch_id||'';
      const plan=j.plan||{}, joint=j.joint_review||{}, manifest=j.commit_manifest||{};
      const items=Array.isArray(plan.items)?plan.items:[];
      const approved=new Set((joint.approved_chapters||[]).map(Number));
      const committed=new Set(items.filter(x=>x&&x.commit_result).map(x=>Number(x.chapter_no)));
      if(!items.length){
        $('auditRepairList').innerHTML='<div class="candidate-empty">修复计划为空。</div>';
      }else{
        $('auditRepairList').innerHTML=items.map((it,idx)=>{
          const n=Number(it.chapter_no||0), meta=it.candidate_meta||{}, commitOptions=it.commit_options||{};
          const hasCandidate=commitOptions.candidate_available===true||Number(meta.chapter_no||0)>0;
          const safe=meta.safe===true||meta.safe===1||(typeof meta.safe==='string'&&['true','1','yes','y'].includes(meta.safe.toLowerCase()));
          const isApproved=approved.has(n), isCommitted=committed.has(n), auto=hasCandidate&&safe&&!!it.auto_commit_allowed&&isApproved;
          const evidenceGate=Array.isArray(it.evidence_gate_reasons)?it.evidence_gate_reasons.filter(Boolean):[];
          const selectable=hasCandidate&&!isCommitted&&safe;
          const forceSelectable=commitOptions.force_selectable===undefined?true:!!commitOptions.force_selectable;
          const forceEligible=hasCandidate&&!isCommitted&&forceSelectable;
          const stateText=isCommitted?'已提交':hasCandidate?(auto?'联合复核通过，可提交':safe?'人工复核，可确认提交':'质量门未通过，仅可强制提交'):(it.channel==='desktop'?'旧计划，需重新生成':it.auto_candidate?(it.channel==='rewrite'?'待生成定向重写候选':'待生成精确补丁候选'):evidenceGate.length?'证据不足，仅人工复核':it.repair_class==='MANUAL_ONLY'?'章节文件不可用':'不回改旧正文');
          const badge=isCommitted?'good':auto?'good':hasCandidate&&!safe?'bad':hasCandidate?'warn':evidenceGate.length||it.channel==='desktop'||it.repair_class==='MANUAL_ONLY'?'warn':'neutral';
          const related=Array.isArray(it.related_chapters)&&it.related_chapters.length?` · 关联章 ${it.related_chapters.join('、')}`:'';
          const gateText=evidenceGate.length?` · ${evidenceGate.join('；')}`:'';
          const forceGateText=!forceSelectable&&hasCandidate?` · ${commitOptions.force_reason||'当前候选不可强制提交'}`:'';
          const selectTitle=!hasCandidate?'尚未生成候选':isCommitted?'本章已提交':!safe?'候选未通过质量门，请使用逐章强制提交':'勾选后可走普通提交或人工确认提交';
          return `<div class="audit-repair-row">
            <label class="audit-repair-check" title="${esc(selectTitle)}"><input type="checkbox" data-repair-select="${n}" data-repair-safe="${safe?'1':'0'}" data-repair-auto="${auto?'1':'0'}" ${auto&&!isCommitted?'checked':''} ${selectable?'':'disabled'} /></label>
            <div class="audit-repair-main">
              <div><b>${n?`第 ${n} 章`:'非正文项'}</b><span class="pill ${badge}">${esc(stateText)}</span></div>
               <small>${esc(auditRepairClassLabel(it.repair_class))}${esc(related)} · ${esc(it.instruction||it.reason||'')}${esc(gateText)}${esc(forceGateText)}</small>
            </div>
            <div class="candidate-actions">
              ${hasCandidate?`<button class="btn ghost" data-repair-view="${n}">对比</button>`:''}
              ${hasCandidate&&!safe?`<button class="btn ghost" data-pro-retry="${n}" data-pro-mode="retry">Pro重新生成</button><button class="btn ghost" data-pro-retry="${n}" data-pro-mode="deep">Pro深度修复</button>`:''}
              ${forceEligible?`<button class="btn danger subtle" data-repair-force="${n}">查看 Diff 后强制提交</button>`:''}
            </div>
          </div>`;
        }).join('');
        $('auditRepairList').querySelectorAll('[data-repair-view]').forEach(b=>b.onclick=()=>openAuditRepairCompare(Number(b.dataset.repairView)));
        $('auditRepairList').querySelectorAll('[data-repair-force]').forEach(b=>b.onclick=()=>openAuditRepairCompare(Number(b.dataset.repairForce),true));
        $('auditRepairList').querySelectorAll('[data-pro-retry]').forEach(b=>b.onclick=()=>singleProRetry(
          window.auditRepairBatchId||'',
          Number(b.dataset.proRetry),
          b.dataset.proMode||'retry'
        ));
      }
      const syncSelectionButtons=()=>{
        const selected=Array.from(document.querySelectorAll('[data-repair-select]:checked'));
        const normal=selected.filter(x=>x.dataset.repairAuto==='1');
        const manual=selected.filter(x=>x.dataset.repairSafe==='1'&&x.dataset.repairAuto!=='1');
        $('auditRepairCommitBtn').disabled=!normal.length;
        $('auditRepairManualBtn').disabled=!manual.length;
      };
      $('auditRepairList').querySelectorAll('[data-repair-select]').forEach(input=>input.onchange=syncSelectionButtons);
      syncSelectionButtons();
      $('auditRepairRollbackBtn').disabled=!(manifest.chapters||[]).length||!!manifest.rolled_back_at;
      if(!silent)toast(`修复批次 ${j.batch_id} 已刷新`,'good');
      return j;
    }catch(e){
      if(e.status===404){
        $('auditRepairList').innerHTML='<div class="candidate-empty">尚无修复批次。</div>';
        $('auditRepairCommitBtn').disabled=true; $('auditRepairManualBtn').disabled=true; $('auditRepairRollbackBtn').disabled=true;
        if(!silent)return null;
      }else if(!silent)toast('读取修复批次失败：'+e.message,'bad');
      return null;
    }
  }

  async function createAuditRepairPlan(){
    const model=$('auditRepairModel').value, audit_text=$('auditRepairText').value.trim();
    try{
      $('auditRepairPlanBtn').disabled=true;
      const j=await post('/api/audit/repair/plan',{audit_text,model});
      toast('已开始生成修复计划','good');
      updateAuditRepair(j.status||{});
      await refreshStatus();
    }catch(e){toast('启动修复计划失败：'+e.message,'bad');}
  }

  async function startAuditRepair(){
    try{
      const j=await post('/api/audit/repair/start',{batch_id:'',model:$('auditRepairModel').value});
      toast('已开始生成候选并进行联合复核','good'); updateAuditRepair(j.status||{}); await refreshStatus();
    }catch(e){toast('启动审计修复失败：'+e.message,'bad');}
  }

  function setComparePayload(payload){
    state.auditCompare=payload||null;
    renderComparePayload();
  }
  async function openAuditRepairCompare(n,focusForce=false){
    try{
      const j=await api(`/api/audit/repair/candidate/${n}`);
      text('chapterCompareTitle',`第 ${n} 章 · 审计修复候选`);
      const m=j.meta||{}, rv=m.review||{};
      const patch_rejections=Array.isArray(m.patch_rejections)?m.patch_rejections:[];
      const rejectionText=patch_rejections.map((x,i)=>`${i+1}. ${x?.reason||'未提供拒绝原因'}`).join('；');
      const operationText=m.channel==='rewrite'||m.generation_mode==='directed_rewrite'?'定向重写':`精确补丁 ${Number(m.patch_meta?.patch_count||0)} 处`;
      text('chapterCompareMeta',`${auditRepairClassLabel(m.repair_class)} · ${operationText} · ${auditRepairAttemptText(m)} · ${m.safe?'自动验收通过':'自动修正未收敛'}${rv.findings?.length?` · ${rv.findings.slice(0,2).join('；')}`:''}${rejectionText?` · 补丁拒绝：${rejectionText}`:''}`);
      const detailForceAvailable=j.force_available!==undefined?!!j.force_available:(m.force_available!==undefined?!!m.force_available:!(j.committed===true||m.committed===true));
      setComparePayload({original:j.original||'',candidate:j.candidate||'',isAudit:true,chapterNo:n,forceAvailable:detailForceAvailable});
      const diffDialog=$('chapterCompareDialog');
      if(diffDialog)diffDialog.dataset.diffClasses='diff-add diff-del diff-change';
      if(!String(j.original||'')&&!String(j.candidate||''))$('chapterCompareOriginal').innerHTML='<span class="diff-empty-state">（无内容）</span>';
      $('chapterCompareDialog').showModal();
      if(focusForce)$('chapterCompareForceBtn')?.focus();
    }catch(e){toast('读取修复候选失败：'+e.message,'bad');}
  }
  function selectedRepairChapters(kind){
    return Array.from(document.querySelectorAll('[data-repair-select]:checked')).map(input=>{
      const n=Number(input.dataset.repairSelect||0);
      return {n,safe:input.dataset.repairSafe==='1',auto:input.dataset.repairAuto==='1'};
    }).filter(row=>row.n>0&&(!kind||(kind==='normal'?row.auto:row.safe&&!row.auto))).map(row=>row.n);
  }
  function auditCommitResultText(result,count,prefix){
    const failed=result?.resummarized?.failed||result?.memory_sync?.failed||result?.sync?.failed||[];
    const headline=count?`${prefix||'已提交'} ${count} 章`:String(prefix||'已提交');
    if(Array.isArray(failed)&&failed.length){
      const detail=failed.map(item=>{
        if(item&&typeof item==='object')return `第 ${item.chapter_no||'?'} 章：${item.error||item.reason||'同步失败'}`;
        return String(item);
      }).join('；');
      return `${headline}；正文已写入，但 Memory/Handoff 同步失败：${detail}`;
    }
    return `${headline}；正文、Summary、Memory、Handoff 已更新`;
  }
  async function commitAuditRepairManual(){
    const chapters=selectedRepairChapters('manual');
    if(!chapters.length)return toast('没有选中可人工确认提交的候选','bad');
    if(!confirm(`确认人工提交 ${chapters.length} 个候选？\n\n这些候选已通过自身质量门，但未获得自动联合/证据批准；提交前仍会校验原文哈希并重建 Memory/Handoff。`))return;
    try{
      const j=await post('/api/audit/repair/commit',{batch_id:'',chapters,manual:true});
      toast(auditCommitResultText(j,j.chapters?.length||chapters.length,'已人工提交修复'),'good');
      await loadAuditRepairBatch(true); await refreshStatus();
    }catch(e){toast('人工提交修复失败：'+e.message,'bad');}
  }
  async function forceAuditRepairChapter(chapterNo){
    const n=Number(chapterNo||state.auditCompare?.chapterNo||0);
    if(!Number.isInteger(n)||n<=0)return toast('章节号错误','bad');
    if(!state.auditCompare||state.auditCompare.chapterNo!==n){await openAuditRepairCompare(n,true);return;}
    const phrase=window.prompt(`第 ${n} 章质量门未通过。已查看上方 Diff 后，如仍要写入请输入 FORCE：`,'');
    if(phrase===null)return;
    if(String(phrase).trim().toUpperCase()!=='FORCE')return toast('未输入 FORCE，已取消强制提交','bad');
    const forceReason=window.prompt('请输入本次强制提交的理由（将写入审计记录）：','人工确认 Diff 后仍需保留该修复');
    if(!forceReason||!String(forceReason).trim())return toast('未填写强制提交理由，已取消','bad');
    try{
      const j=await post('/api/audit/repair/commit',{batch_id:'',chapters:[n],force:true,confirm:'FORCE',force_reason:String(forceReason).trim()});
      toast(auditCommitResultText(j,0,`已强制提交第 ${j.chapters?.[0]||n} 章修复`),'good');
      if($('chapterCompareDialog')?.open)$('chapterCompareDialog').close();
      await loadAuditRepairBatch(true); await refreshStatus();
    }catch(e){toast('强制提交失败：'+e.message,'bad');}
  }

  async function commitAuditRepair(){
    const chapters=selectedRepairChapters('normal');
    if(!chapters.length)return toast('没有选中联合复核通过的候选','bad');
    if(!confirm(`确认提交 ${chapters.length} 个联合复核通过候选？\n\n提交前会先完整备份，并重建正文、Summary、Memory、Handoff。`))return;
    try{
      const j=await post('/api/audit/repair/commit',{batch_id:'',chapters,manual:false});
      toast(auditCommitResultText(j,j.chapters?.length||chapters.length,'已提交修复'),'good'); await loadAuditRepairBatch(true); await refreshStatus();
    }catch(e){toast('提交修复失败：'+e.message,'bad');}
  }

  async function rollbackAuditRepair(){
    if(!confirm('确认回滚本批次已经提交的全部章节？\n\n如果提交后任一目标章又被人工修改，系统会拒绝整批回滚。'))return;
    try{
      const j=await post('/api/audit/repair/rollback',{batch_id:''});
      toast(`已回滚 ${j.chapters.length} 章`,'good'); await loadAuditRepairBatch(true); await refreshStatus();
    }catch(e){toast('回滚失败：'+e.message,'bad');}
  }

  async function loadExportInfo(){
    try{
      const j=await api('/api/chapters/export-info');
      if(j.count){
        $('exportStart').value=j.first||1; $('exportEnd').value=j.last||j.first||1;
        if($('auditStart')) $('auditStart').value=j.first||1; if($('auditEnd')) $('auditEnd').value=j.last||j.first||1;
        setPill('exportPill',`${fmtInt(j.count)} 章`,'good');
        text('exportNotice',`已生成 ${j.count} 章（第 ${j.first}～${j.last} 章）。导出时缺失章节自动跳过，原文件不修改。`);
      }else{
        setPill('exportPill','暂无章节','neutral');
        text('exportNotice','chapters/ 中尚无已完成的 NNNN.md。');
      }
    }catch(e){ setPill('exportPill','读取失败','bad'); text('exportNotice','读取章节范围失败：'+e.message); }
  }

  async function exportChapters(fmt){
    const start=Math.round(Number($('exportStart').value||0)), end=Math.round(Number($('exportEnd').value||0));
    if(start<1||end<1||start>end)return toast('导出章节范围无效','bad');
    const buttons=[$('exportMdBtn'),$('exportTxtBtn'),$('exportZipBtn')]; buttons.forEach(b=>b.disabled=true);
    try{
      const r=await fetch(`/api/chapters/export?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&format=${encodeURIComponent(fmt)}`,{credentials:'same-origin'});
      if(r.status===401){location.reload();throw new Error('未登录');}
      if(!r.ok){const j=await r.json().catch(()=>({detail:r.statusText||`HTTP ${r.status}`}));throw new Error(typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||j));}
      const blob=await r.blob(); const url=URL.createObjectURL(blob); const a=document.createElement('a');
      a.href=url; a.download=`chapters_${String(start).padStart(4,'0')}-${String(end).padStart(4,'0')}.${fmt}`; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1500);
      const count=Number(r.headers.get('X-NovelAgent-Export-Count')||0), missing=Number(r.headers.get('X-NovelAgent-Missing-Count')||0);
      toast(`已导出 ${count} 章${missing?`，跳过 ${missing} 个缺失章节`:''}`,'good');
    }catch(e){toast('导出失败：'+e.message,'bad');}
    finally{buttons.forEach(b=>b.disabled=false);}
  }

  async function loadReaderReflowInfo(){
    try{
      const j=await api('/api/reader-reflow/info');
      if(j.source_count){
        if(!$('readerReflowStart').value||Number($('readerReflowStart').value)===1)$('readerReflowStart').value=j.source_first||1;
        if(!$('readerReflowEnd').value||Number($('readerReflowEnd').value)===1)$('readerReflowEnd').value=j.source_last||j.source_first||1;
        if(!j.status?.running && !j.status?.completed)setPill('readerReflowPill',j.reader_count?`${fmtInt(j.reader_count)} 章已生成`:'尚未生成',j.reader_count?'good':'neutral');
        if(!j.status?.running && !j.status?.completed)text('readerReflowNotice',`Canon 共 ${j.source_count} 章；已有 ${j.reader_count} 章读者版。输出只写入 reader_chapters/。`);
      }else{
        setPill('readerReflowPill','暂无 Canon','warn');
        text('readerReflowNotice','chapters/ 中没有可处理的 NNNN.md。');
      }
      updateReaderReflow(j.status||{});
    }catch(e){setPill('readerReflowPill','读取失败','bad');text('readerReflowNotice','读取智能分段状态失败：'+e.message);}
  }

  async function startReaderReflow(){
    const start=Math.round(Number($('readerReflowStart').value||0)), end=Math.round(Number($('readerReflowEnd').value||0));
    const workers=Math.max(1,Math.min(8,Math.round(Number($('readerReflowWorkers').value||4))));
    const overwrite=!!$('readerReflowOverwrite').checked;
    if(start<1||end<start)return toast('智能分段章节范围无效','bad');
    const extra=overwrite?'\n\n已勾选覆盖：现有读者版会重新生成。':'';
    if(!confirm(`确认用 DeepSeek V4 Flash 智能分段第 ${start}—${end} 章？\n\n原 Canon 永不覆盖；只允许改变段落换行。${extra}`))return;
    try{
      const j=await post('/api/reader-reflow/start',{start,end,workers,overwrite});
      updateReaderReflow(j.status||{});toast('读者版智能分段已启动','good');
    }catch(e){toast('启动失败：'+e.message,'bad');}
  }

  async function exportReaderReflow(fmt){
    const start=Math.round(Number($('readerReflowStart').value||0)), end=Math.round(Number($('readerReflowEnd').value||0));
    if(start<1||end<start)return toast('导出章节范围无效','bad');
    const buttons=[$('readerExportTxtBtn'),$('readerExportMdBtn'),$('readerExportZipBtn')];buttons.forEach(b=>b.disabled=true);
    try{
      const r=await fetch(`/api/reader-reflow/export?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&format=${encodeURIComponent(fmt)}`,{credentials:'same-origin'});
      if(r.status===401){location.reload();throw new Error('未登录');}
      if(!r.ok){const j=await r.json().catch(()=>({detail:r.statusText||`HTTP ${r.status}`}));throw new Error(typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||j));}
      const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
      a.href=url;a.download=`reader_chapters_${String(start).padStart(4,'0')}-${String(end).padStart(4,'0')}.${fmt}`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1500);
      const count=Number(r.headers.get('X-NovelAgent-Export-Count')||0),missing=Number(r.headers.get('X-NovelAgent-Missing-Count')||0);
      toast(`已导出 ${count} 章读者版${missing?`，跳过 ${missing} 个缺失章节`:''}`,'good');
    }catch(e){toast('读者版导出失败：'+e.message,'bad');}
    finally{buttons.forEach(b=>b.disabled=false);}
  }

  async function viewReaderReflow(){
    const n=Number(state.status?.reader_reflow?.last_output_chapter||$('readerReflowStart').value||0);
    if(!n)return toast('尚无可查看的读者版章节','bad');
    try{
      const j=await api(`/api/reader-reflow/chapter/${n}`),m=j.meta||{};
      openViewer(`第 ${n} 章读者版 · ${m.source_paragraphs||'?'}→${m.reader_paragraphs||'?'} 段`,j.reader||'');
    }catch(e){toast('读取读者版失败：'+e.message,'bad');}
  }

  async function loadPromptList(force=false){
    if(state.promptLoaded&&!force)return;
    try{
      const j=await api('/api/prompts'), files=Array.isArray(j.files)?j.files:[]; const sel=$('promptFileSelect'); const current=sel.value;
      sel.innerHTML=files.length?files.map(x=>`<option value="${esc(x.name)}">${esc(x.name)} · ${fmtInt(x.chars)} 字符</option>`).join(''):'<option value="">story/ 下没有 .md</option>';
      state.promptLoaded=true;
      if(current&&files.some(x=>x.name===current))sel.value=current;
      if(files.length)await loadPromptFile(sel.value||files[0].name,true); else{$('promptEditorText').value='';state.promptOriginal='';text('promptEditorNotice','story/ 下没有可编辑的 .md 文件。');}
    }catch(e){toast('读取提示词列表失败：'+e.message,'bad');text('promptEditorNotice','读取失败：'+e.message);}
  }

  async function loadPromptFile(name, silent=false){
    name=name||$('promptFileSelect').value; if(!name)return;
    if(!silent && state.promptOriginal!=='' && $('promptEditorText').value!==state.promptOriginal && !confirm('当前提示词有未保存修改。确认丢弃并重新载入吗？'))return;
    try{
      const j=await api(`/api/prompts/${encodeURIComponent(name)}`); $('promptEditorText').value=j.content||''; state.promptOriginal=j.content||'';
      text('promptEditorNotice',`${j.name} 已载入。保存会先备份旧版本；Canon/DLC 运行时禁止保存。`);
    }catch(e){toast('读取提示词失败：'+e.message,'bad');}
  }

  async function savePromptFile(){
    const name=$('promptFileSelect').value, content=$('promptEditorText').value; if(!name)return toast('请先选择提示词文件','bad');
    if(content===state.promptOriginal)return toast('内容没有变化','');
    if(!confirm(`确认保存 ${name}？\n\n旧版本会自动备份到 runtime/prompt_backups/。`))return;
    try{
      const j=await post('/api/prompts/save',{name,content}); state.promptOriginal=content;
      text('promptEditorNotice',j.changed?`${name} 已保存；备份：${j.backup}`:`${name} 未发生变化。`); toast(`${name} 已保存`,'good');
      await loadPromptList(true);
    }catch(e){toast('保存提示词失败：'+e.message,'bad');text('promptEditorNotice','保存失败：'+e.message);}
  }

  function setMdRoute(data={}){
    text('mdManagerFile',data.file||'—');
    text('mdManagerTarget',data.target||'—');
    text('mdManagerSection',data.section||'—');
    text('mdManagerAction',data.action||'—');
  }

  function resetMdPreview(clearInput=false){
    if(clearInput)$('mdManagerInput').value='';
    state.mdBeforeHash=null;
    setMdRoute({});
    text('mdManagerDiff','尚未生成预览。');
    text('mdManagerDiffMeta','Unified Diff');
    text('mdManagerNotice',clearInput?'已清空。':'路由内容已变化，请重新检查修改。');
    setPill('mdManagerPill',clearInput?'等待粘贴':'需要重新预览',clearInput?'neutral':'warn');
    $('mdManagerCommitBtn').disabled=true;
  }

  async function mdManagerPaste(){
    try{
      if(!navigator.clipboard?.readText)throw new Error('Clipboard API unavailable');
      const value=await navigator.clipboard.readText();
      $('mdManagerInput').value=value||'';
      resetMdPreview(false);
      text('mdManagerNotice','已读取系统剪贴板；请点击“检查修改”。');
      setPill('mdManagerPill','已粘贴','good');
    }catch(e){
      text('mdManagerNotice','浏览器未允许直接读取剪贴板。安卓请长按上方文本框 → 粘贴，然后点击“检查修改”。');
      setPill('mdManagerPill','请手动粘贴','warn');
    }
  }

  async function mdManagerParse(){
    const value=$('mdManagerInput').value;
    if(!value.trim())return toast('请先粘贴 GPT 输出','bad');
    try{
      const j=await post('/api/md-manager/parse',{text:value});
      setMdRoute(j);
      state.mdBeforeHash=null;
      $('mdManagerCommitBtn').disabled=true;
      text('mdManagerDiff','尚未生成预览。');
      text('mdManagerNotice','路由解析成功。下一步点击“检查修改”。');
      setPill('mdManagerPill','路由已识别','good');
    }catch(e){
      setPill('mdManagerPill','解析失败','bad');
      text('mdManagerNotice',e.message);
      toast('MD 路由解析失败：'+e.message,'bad');
    }
  }

  async function mdManagerPreview(){
    const value=$('mdManagerInput').value;
    if(!value.trim())return toast('请先粘贴 GPT 输出','bad');
    $('mdManagerPreviewBtn').disabled=true;
    try{
      const j=await post('/api/md-manager/preview',{text:value});
      setMdRoute(j);
      state.mdBeforeHash=j.before_hash||null;
      $('mdManagerCommitBtn').disabled=!state.mdBeforeHash;
      $('mdManagerDiff').textContent=j.diff||'没有可显示的 Diff。';
      text('mdManagerDiffMeta',`${fmtInt(j.before_chars)} → ${fmtInt(j.after_chars)} 字符`);
      text('mdManagerNotice',j.note||'预览完成。确认 Diff 正确后再写入。');
      setPill('mdManagerPill','等待确认','warn');
    }catch(e){
      state.mdBeforeHash=null;
      $('mdManagerCommitBtn').disabled=true;
      text('mdManagerDiff','预览失败。');
      text('mdManagerNotice',e.message);
      setPill('mdManagerPill','预览失败','bad');
      toast('MD 预览失败：'+e.message,'bad');
    }finally{
      $('mdManagerPreviewBtn').disabled=false;
    }
  }

  async function mdManagerCommit(){
    const value=$('mdManagerInput').value;
    if(!state.mdBeforeHash)return toast('请先点击“检查修改”','bad');
    const file=$('mdManagerFile').textContent||'story MD';
    const target=$('mdManagerTarget').textContent||'—';
    const action=$('mdManagerAction').textContent||'—';
    if(!confirm(`确认写入 ${file}？\n\n目标：${target}\n操作：${action}\n\n写入前会自动备份原文件。`))return;

    $('mdManagerCommitBtn').disabled=true;
    try{
      const j=await post('/api/md-manager/commit',{text:value,before_hash:state.mdBeforeHash});
      state.mdBeforeHash=null;
      text('mdManagerNotice',j.backup?`写入成功。备份：${j.backup}`:'写入成功。');
      setPill('mdManagerPill','写入成功','good');
      toast(`${j.file} 已安全写入`,'good');
      text('mdManagerDiffMeta','已提交；再次修改请重新预览');
    }catch(e){
      text('mdManagerNotice',e.message);
      setPill('mdManagerPill','写入失败','bad');
      toast('MD 写入失败：'+e.message,'bad');
      state.mdBeforeHash=null;
    }
  }

  async function refreshStatus(){
    try{
      const s=await api('/api/status'); state.status=s; setPill('connectionPill','已连接','good');
      text('versionBadge',`V${s.version||'5.8.0'}`); updatePricing(s.deepseek_pricing); updateCanon(s); updateDLC(s.dlc); updateAudit(s.audit); updateAuditRepair(s.audit_repair); updateReaderReflow(s.reader_reflow); updateServices(s); renderRecent(s.recent_chapters);
    }catch(e){ setPill('connectionPill','连接失败','bad'); }
  }

  async function loadConfig(){
    try{ const c=await api('/api/config'); state.config=c; text('novelTitle',c.title||'小说生产控制台');
      $('cfgCount').value=c.chapters_per_run??1; $('cfgChars').value=c.target_chapter_chars??4500; $('cfgRecent').value=c.recent_summary_count??5; $('cfgRevision').value=c.max_revision_rounds??1; $('cfgTopK').value=c.retrieval_top_k??14; $('cfgMinScore').value=c.retrieval_min_score??0.22; $('cfgPlanContextTrim').checked=c.plan_context_trim_enabled!==false;
      $('cfgPlanRecoveryTarget').value=c.plan_context_recovery_target_tokens??38000; $('cfgPlanRecoveryMax').value=c.plan_context_recovery_max_tokens??42000; $('cfgCostGuardMode').value=c.chapter_cost_guard_mode||'afp'; $('cfgCostGuardAfp').value=c.chapter_cost_guard_afp_limit??20; $('cfgCostGuardCny').value=c.chapter_cost_guard_cny_limit??5; syncCostGuardFields();
    }catch(e){toast('读取配置失败：'+e.message,'bad');}
  }
  function syncCostGuardFields(){ const mode=$('cfgCostGuardMode').value; $('cfgCostGuardLimits').hidden=mode==='unlimited'; $('cfgCostGuardAfpWrap').hidden=mode!=='afp'; $('cfgCostGuardCnyWrap').hidden=mode!=='cny'; }
  async function loadRouting(){
    try{
      const r=await api('/api/routing'); state.routing=r;
      const order=['plan','draft','review','deep_review','revision','summary','memory'];
      const names={plan:'Plan',draft:'Draft',review:'Review',deep_review:'Deep Review',revision:'Revision',summary:'Summary',memory:'Memory'};
      const defaults={plan:{model:'deepseek-v4-pro',thinking:true,reasoning_effort:'low'},draft:{model:'deepseek-v4-flash',thinking:false,reasoning_effort:'low'},review:{model:'deepseek-v4-flash',thinking:true,reasoning_effort:'low'},deep_review:{model:'deepseek-v4-pro',thinking:true,reasoning_effort:'high'},revision:{model:'deepseek-v4-flash',thinking:false,reasoning_effort:'low'},summary:{model:'deepseek-v4-flash',thinking:false,reasoning_effort:'low'},memory:{model:'deepseek-v4-flash',thinking:false,reasoning_effort:'low'}};
      const stages=r.canon_stages||{};
      const profile=r.cost_control?.profile||'enhanced';
      const selected=['quality','enhanced','balanced','saving','custom'].includes(profile)?profile:'custom';
      $('costProfile').value=selected;
      $('customRoutePanel').hidden=selected!=='custom';
      const shortModel=x=>(String(x?.model||'').includes('pro')?'Pro':'Flash')+(x?.thinking?` + Thinking ${x?.reasoning_effort||'low'}`:'');
      text('costProfileSummary',`当前实际：Plan ${shortModel(stages.plan||defaults.plan)} · Draft ${shortModel(stages.draft||defaults.draft)} · Review ${shortModel(stages.review||defaults.review)} · Deep Review ${shortModel(stages.deep_review||defaults.deep_review)} · Revision ${shortModel(stages.revision||defaults.revision)} · Summary ${shortModel(stages.summary||defaults.summary)} · Memory ${shortModel(stages.memory||defaults.memory)}`);
      $('routeList').innerHTML=`<div class="route-item"><span>Canon 权限</span><b>DeepSeek Only（Memory 可本地提取）</b></div>`+order.map(k=>{
        const x={...defaults[k],...(stages[k]||{})};
        const eff=x.reasoning_effort==='medium'?'high':(x.reasoning_effort||'low');
        return `<div class="route-edit-row" data-stage="${k}"><span>${names[k]}</span><select class="canon-model"><option value="deepseek-v4-flash" ${x.model==='deepseek-v4-flash'?'selected':''}>V4 Flash</option><option value="deepseek-v4-pro" ${x.model==='deepseek-v4-pro'?'selected':''}>V4 Pro</option></select><label class="thinking-check"><input class="canon-thinking" type="checkbox" ${x.thinking?'checked':''}/> Thinking</label><select class="canon-effort" title="Thinking 强度"><option value="low" ${eff==='low'?'selected':''}>low</option><option value="high" ${eff==='high'?'selected':''}>high</option><option value="max" ${eff==='max'?'selected':''}>max</option></select></div>`;
      }).join('');
      $('routeList').querySelectorAll('.route-edit-row').forEach(row=>{const cb=row.querySelector('.canon-thinking'),eff=row.querySelector('.canon-effort'); const sync=()=>eff.disabled=!cb.checked||!!state.status?.running; cb.onchange=sync; sync();});
      const disabled=!!state.status?.running; $('routeList').querySelectorAll('.canon-model,.canon-thinking').forEach(el=>el.disabled=disabled); $('canonRouteSaveBtn').disabled=disabled; $('costProfileBtn').disabled=disabled; $('costProfile').disabled=disabled;
    }catch(e){$('routeList').innerHTML='<div class="notice">路由读取失败</div>';}
  }
  async function saveCanonRouting(){
    const stages={}; document.querySelectorAll('#routeList .route-edit-row[data-stage]').forEach(row=>{stages[row.dataset.stage]={model:row.querySelector('.canon-model').value,thinking:row.querySelector('.canon-thinking').checked,reasoning_effort:row.querySelector('.canon-effort').value};});
    if(!Object.keys(stages).length)return toast('Canon 路由尚未加载','bad');
    await withAction(()=>post('/api/canon/routing',{stages}),'Canon 自定义路由已保存'); await loadRouting();
  }
  async function applyCostProfile(){
    const profile=$('costProfile').value;
    await withAction(()=>post('/api/cost/profile',{profile}),profile==='custom'?'已进入自定义模式':`已应用${$('costProfile').selectedOptions[0].textContent}`);
    await loadRouting();
  }
  async function loadGrokConfig(){
    try{
      const c=await api('/api/grok/config');
      $('grokModel').value=c.model||'grok-4.6'; $('grokReviewModel').value=c.review_model||'grok-4.6';
      $('grokEffort').value=c.reasoning_effort||'low'; $('grokReviewEffort').value=c.review_reasoning_effort||'medium';
      $('grokReviewEnabled').checked=c.review_enabled!==false;
      text('grokState',`${c.configured?'Key 已配置':'Key 未配置'} · ${c.model||'grok-4.6'} · 参考资料${c.atlas_exists?'已找到':'缺失'}：${c.atlas_file||'—'}`);
    }catch(e){text('grokState','读取 Grok 设置失败：'+e.message);}
  }
  async function saveGrokKey(){
    const api_key=$('grokApiKey').value.trim(); if(!api_key)return toast('请输入 Grok API Key','bad');
    try{await post('/api/grok/key',{api_key});$('grokApiKey').value='';toast('Grok API Key 已加密保存','good');await loadGrokConfig();}catch(e){toast('保存 Grok Key 失败：'+e.message,'bad');}
  }
  async function saveGrokConfig(){
    try{await post('/api/grok/config',{model:$('grokModel').value,review_model:$('grokReviewModel').value,reasoning_effort:$('grokEffort').value,review_reasoning_effort:$('grokReviewEffort').value,review_enabled:$('grokReviewEnabled').checked});toast('Grok DLC 设置已保存','good');await loadGrokConfig();}catch(e){toast('保存 Grok 设置失败：'+e.message,'bad');}
  }
  async function testGrok(){
    try{const j=await post('/api/grok/test');toast(j.detail||'Grok 连接正常','good');await loadGrokConfig();}catch(e){toast('Grok 连接失败：'+e.message,'bad');}
  }
  async function scanDLC(){
    const n=Number($('dlcChapter').value); if(!n)return toast('请输入 Canon 章节','bad');
    try{ const j=await api(`/api/dlc/markers/${n}`); const sel=$('dlcScene'); sel.innerHTML='';
      if(!j.markers?.length){sel.innerHTML='<option value="">本章没有 DLC_SCENE 标记</option>';$('dlcCandidates').innerHTML='<div class="candidate-empty">本章没有 DLC 场景。</div>';toast(`第 ${n} 章没有 DLC 标记`);return;}
      j.markers.forEach(m=>{const o=document.createElement('option');o.value=m.id;o.disabled=!m.eligible;o.textContent=`${m.id} · ${m.type}${m.candidate_count?` · ${m.candidate_count}候选`:''}${m.eligible?'':' · 不可生成'}`; if(!m.eligible)o.title=m.blocked_reason||'';sel.appendChild(o);});
      toast(`找到 ${j.markers.length} 个 DLC 标记`,'good'); await loadDLCCandidates();
    }catch(e){toast('扫描失败：'+e.message,'bad');}
  }
  async function loadDLCCandidates(){
    const n=Number($('dlcChapter').value), sid=$('dlcScene').value, box=$('dlcCandidates');
    if(!n||!sid){box.innerHTML='<div class="candidate-empty">扫描章节并选择场景后显示候选。</div>';return;}
    try{
      const j=await api(`/api/dlc/candidates/${n}/${encodeURIComponent(sid)}`), rows=j.candidates||[];
      if(!rows.length){box.innerHTML='<div class="candidate-empty">还没有候选。设置抽奖次数后点击“开始抽奖”。</div>';return;}
      box.innerHTML='';
      rows.forEach(c=>{
        const row=document.createElement('div'); row.className='candidate-row'+(c.selected?' selected':'');
        const name=document.createElement('div'); name.className='candidate-name'; name.textContent=`${c.selected?'★ ':''}${c.label||c.candidate_id}`;
        const chars=document.createElement('div'); chars.className='candidate-meta'; chars.textContent=`${fmtInt(c.chars)} 字`;
        const statusLabel=c.stale?'⚠ Canon 已变更':c.review_status==='passed'?'✓ 严格审查通过':c.review_status==='blocked'?'⛔ 已拦截':'⚠ 旧版未审查';
        const when=document.createElement('div'); when.className='candidate-meta'+((c.stale||c.review_status!=='passed')?' candidate-bad':''); when.textContent=`${statusLabel}${c.generated_at?` · ${c.generated_at}`:''}`;
        when.title=[c.review_summary||'',...(c.review_violations||[])].filter(Boolean).join('\n');
        const act=document.createElement('div'); act.className='candidate-actions';
        const preview=document.createElement('button'); preview.className='btn ghost'; preview.textContent='预览'; preview.onclick=()=>readDLC(c.candidate_id);
        const select=document.createElement('button'); select.className='btn ghost'; select.textContent=c.selected?'已选中':(c.selectable?'选中':'禁止选中'); select.disabled=!!c.selected||!c.selectable; select.title=c.selectable?'':(c.review_summary||'该候选未通过严格审查'); select.onclick=()=>selectDLCCandidate(c.candidate_id);
        const del=document.createElement('button'); del.className='btn danger subtle'; del.textContent='删除'; del.onclick=()=>deleteDLCCandidate(c.candidate_id);
        act.append(preview,select,del); row.append(name,chars,when,act); box.appendChild(row);
      });
    }catch(e){box.innerHTML=`<div class="candidate-empty">读取候选失败：${esc(e.message)}</div>`;}
  }
  async function generateDLC(){
    const chapter_no=Number($('dlcChapter').value), scene_id=$('dlcScene').value, custom_prompt=$('dlcPrompt').value.trim(), max_tokens=Number($('dlcMaxTokens').value||0), draw_count=Number($('dlcDrawCount').value||1);
    if(!chapter_no||!scene_id)return toast('请先选择 DLC 场景','bad');
    if(!Number.isInteger(draw_count)||draw_count<1||draw_count>20)return toast('抽奖次数请输入 1～20 的整数','bad');
    try{ if(custom_prompt){localStorage.setItem('na_dlc_last_prompt',custom_prompt);state.lastPrompt=custom_prompt;} await post('/api/dlc/generate',{chapter_no,scene_id,custom_prompt,max_tokens,draw_count}); toast(`Grok DLC ${scene_id} 开始抽 ${draw_count} 次`,'good'); refreshStatus(); }
    catch(e){toast('DLC 启动失败：'+e.message,'bad');}
  }
  async function readDLC(candidateId=''){
    const n=Number($('dlcChapter').value), sid=$('dlcScene').value; if(!n||!sid)return toast('请先选择 DLC 场景','bad');
    const q=candidateId?`?candidate_id=${encodeURIComponent(candidateId)}`:'';
    try{const j=await api(`/api/dlc/read/${n}/${encodeURIComponent(sid)}${q}`);const rs=j.review_status==='passed'?' · ✓ 审查通过':j.review_status==='blocked'?' · ⛔ 已拦截':' · ⚠ 未审查';openViewer(`第 ${n} 章 · ${sid} · ${j.candidate_id||'当前'}${j.selected?' · ★ 已选中':''}${j.stale?' · ⚠ Canon 已变更':''}${rs}`,j.text||'');}catch(e){toast('读取 DLC 失败：'+e.message,'bad');}
  }
  async function selectDLCCandidate(candidate_id){
    const chapter_no=Number($('dlcChapter').value), scene_id=$('dlcScene').value;
    try{await post('/api/dlc/candidate/select',{chapter_no,scene_id,candidate_id});toast(`${candidate_id} 已选中`,'good');await loadDLCCandidates();}catch(e){toast('选中失败：'+e.message,'bad');}
  }
  async function deleteDLCCandidate(candidate_id){
    const chapter_no=Number($('dlcChapter').value), scene_id=$('dlcScene').value;
    if(!confirm(`确认删除 ${candidate_id}？此操作不会影响 Canon。`))return;
    try{await post('/api/dlc/candidate/delete',{chapter_no,scene_id,candidate_id});toast(`${candidate_id} 已删除`,'good');await loadDLCCandidates();}catch(e){toast('删除失败：'+e.message,'bad');}
  }
  function openViewer(title,body){text('viewerTitle',title);text('viewerBody',body);$('viewerDialog').showModal();}

  async function openChapterCompare(mode){
    const n=Number($('toolChapter').value);
    if(!n)return toast('请输入章节号','bad');
    mode=mode||$('toolCandidateMode').value;
    try{
      const j=await api(`/api/chapter/candidate/${n}/${encodeURIComponent(mode)}`);
      const label={expand:'扩写',rewrite:'改写',polish:'润色',minor:'小幅重修'}[mode]||mode;
      text('chapterCompareTitle',`第 ${n} 章 · ${label}候选`);
      const review=j.review||{};
      let meta='候选已生成';
      if(review.safe_to_accept===true)meta+=' · 一致性检查：可接受';
      else if(review.safe_to_accept===false)meta+=' · 一致性检查：需要检查';
      const changes=Array.isArray(review.changes)?review.changes.filter(Boolean):[];
      if(changes.length)meta+=` · ${changes.slice(0,3).join('；')}`;
      text('chapterCompareMeta',meta);
      setComparePayload({original:j.original||'',candidate:j.candidate||'',isAudit:false,chapterNo:n,forceAvailable:false});
      $('chapterCompareDialog').showModal();
    }catch(e){toast('读取候选失败：'+e.message,'bad');}
  }
  async function viewChapter(n){ try{const j=await api(`/api/chapter/${n}`); const body=j.final||j.text||j.chapter||JSON.stringify(j,null,2);openViewer(`第 ${n} 章 Canon`,typeof body==='string'?body:JSON.stringify(body,null,2));}catch(e){toast('读取章节失败：'+e.message,'bad');} }

  function appendLog(line){ const box=$('logBox'); const t=new Date().toLocaleTimeString('zh-CN',{hour12:false}); box.textContent += `[${t}] ${line}\n`; if(box.textContent.length>180000)box.textContent=box.textContent.slice(-140000); if($('autoScrollToggle').checked)box.scrollTop=box.scrollHeight; }
  function connectEvents(){
    if(state.eventSource)state.eventSource.close(); const es=new EventSource('/api/events');state.eventSource=es;
    es.onmessage=(ev)=>{try{const x=JSON.parse(ev.data); if(x.type==='log')appendLog(x.text); else if(x.type==='stage')appendLog(`阶段：第${x.chapter}章 ${x.stage}${x.label?' · '+x.label:''}`); else if(x.type==='canon_output'){if($('liveToggle').checked)appendPreview('canonPreview',x.text||'');} else if(x.type==='dlc_output')appendPreview('dlcPreview',x.text||''); else if(x.type==='dlc_candidate_started'){setPreview('dlcPreview','','尚未收到 DLC 输出。');appendLog(`DLC 候选开始：${x.candidate_id} (${x.draw_index}/${x.draw_count})`);} else if(x.type==='dlc_candidate_finished'){appendLog(`DLC 候选${x.review_status==='passed'?'通过':'拦截'}：${x.candidate_id} (${x.draw_index}/${x.draw_count})`);loadDLCCandidates();} else if(x.type==='dlc_finished'){appendLog(`DLC 任务结束：第${x.chapter}章 ${x.scene_id}`);loadDLCCandidates();}}catch{}};
    es.onerror=()=>{};
  }

  async function withAction(fn,success){ try{const r=await fn(); if(success)toast(success,'good'); await refreshStatus(); return r;}catch(e){toast(e.message,'bad');} }
  function bind(){
    $('refreshBtn').onclick=()=>Promise.all([refreshStatus(),loadConfig(),loadRouting(),loadDeepSeekConfig(),loadGrokConfig(),refreshProviderStatuses()]);
    $('logoutBtn').onclick=()=>withAction(()=>post('/api/auth/logout'), '已退出').then(()=>location.reload());
    $('canonStartBtn').onclick=startCanon;
    $('canonStopBtn').onclick=()=>withAction(()=>post('/api/stop'),'已发送 Canon 停止请求');
    $('highContextToggle').onchange=setHighContextMode;
    $('planOverflowContinueBtn').onclick=continuePlanOverflow;
    $('planOverflowCancelBtn').onclick=cancelPlanOverflow;
    $('liveToggle').onchange=()=>{const enabled=$('liveToggle').checked;if($('canonPreviewBlock'))$('canonPreviewBlock').hidden=!enabled;if(!enabled&&$('canonPreview'))$('canonPreview').textContent='尚未收到正文输出。';withAction(()=>post('/api/live',{enabled}));};
    $('dlcScanBtn').onclick=scanDLC; $('dlcGenerateBtn').onclick=generateDLC; $('dlcStopBtn').onclick=()=>withAction(()=>post('/api/dlc/stop'),'已发送 DLC 停止请求'); $('dlcReadBtn').onclick=()=>readDLC(); $('dlcRefreshCandidatesBtn').onclick=loadDLCCandidates; $('dlcScene').onchange=loadDLCCandidates;
    $('reusePromptBtn').onclick=()=>{$('dlcPrompt').value=localStorage.getItem('na_dlc_last_prompt')||'';}; $('clearPromptBtn').onclick=()=>{$('dlcPrompt').value='';}; $('dlcDrawCount').onchange=()=>{const n=Math.max(1,Math.min(20,Number($('dlcDrawCount').value||1)));$('dlcDrawCount').value=Math.round(n);localStorage.setItem('na_dlc_draw_count',String(Math.round(n)));};
    $('grokKeySaveBtn').onclick=saveGrokKey; $('grokConfigSaveBtn').onclick=saveGrokConfig; $('grokTestBtn').onclick=testGrok;
    $('viewerClose').onclick=()=>$('viewerDialog').close();
    $('chapterCompareClose').onclick=()=>$('chapterCompareDialog').close();
    $('chapterCompareForceBtn').onclick=()=>forceAuditRepairChapter(state.auditCompare?.chapterNo||0);
    $('chapterCompareChangedOnly').onchange=renderComparePayload;
    $('clearLogBtn').onclick=()=>{$('logBox').textContent='';};
    $('cfgCostGuardMode').onchange=syncCostGuardFields;
    $('configSaveBtn').onclick=()=>withAction(()=>post('/api/config',{chapters_per_run:Number($('cfgCount').value),target_chapter_chars:Number($('cfgChars').value),recent_summary_count:Number($('cfgRecent').value),max_revision_rounds:Number($('cfgRevision').value),retrieval_top_k:Number($('cfgTopK').value),retrieval_min_score:Number($('cfgMinScore').value),plan_context_trim_enabled:$('cfgPlanContextTrim').checked,plan_context_recovery_target_tokens:Number($('cfgPlanRecoveryTarget').value),plan_context_recovery_max_tokens:Number($('cfgPlanRecoveryMax').value),chapter_cost_guard_mode:$('cfgCostGuardMode').value,chapter_cost_guard_afp_limit:Number($('cfgCostGuardAfp').value||0),chapter_cost_guard_cny_limit:Number($('cfgCostGuardCny').value||0)}),'生成参数已保存');
    $('canonRouteSaveBtn').onclick=saveCanonRouting; $('costProfileBtn').onclick=applyCostProfile;
    $('costProfile').onchange=()=>{ $('customRoutePanel').hidden=$('costProfile').value!=='custom'; };
    const chapterEdit=async(mode)=>{
      const label={expand:'扩写',rewrite:'改写',polish:'润色',minor:'小幅重修'}[mode]||mode;
      const r=await withAction(()=>post('/api/chapter/edit',{chapter_no:Number($('toolChapter').value),mode,target_chars:Number($('toolTargetChars').value||0),instruction:$('toolInstruction').value.trim(),provider:'auto',model:'',thinking:null}),`${label}候选已生成`);
      if(r){$('toolCandidateMode').value=mode;await openChapterCompare(mode);}
      return r;
    };
    $('toolExpandBtn').onclick=()=>chapterEdit('expand');
    $('toolRewriteCandidateBtn').onclick=()=>chapterEdit('rewrite');
    $('toolPolishBtn').onclick=()=>chapterEdit('polish');
    $('toolMinorBtn').onclick=()=>chapterEdit('minor');
    $('toolCompareBtn').onclick=()=>openChapterCompare($('toolCandidateMode').value);
    $('toolRewriteFromBtn').onclick=()=>{const n=Number($('toolChapter').value);if(!n)return toast('请输入章节号','bad');if(confirm(`确认从第 ${n} 章开始回档并重新生成后续章节？这会移除第 ${n} 章及以后的活动内容。`))withAction(()=>post('/api/chapter/rewrite',{chapter_no:n}),'已启动回档重写流程');};
    $('toolAcceptExpandBtn').onclick=()=>withAction(()=>post('/api/chapter/candidate/accept',{chapter_no:Number($('toolChapter').value),mode:'expand'}),'已接受扩写候选');
    $('toolAcceptRewriteBtn').onclick=()=>withAction(()=>post('/api/chapter/candidate/accept',{chapter_no:Number($('toolChapter').value),mode:'rewrite'}),'已接受改写候选');
    $('toolAcceptPolishBtn').onclick=()=>withAction(()=>post('/api/chapter/candidate/accept',{chapter_no:Number($('toolChapter').value),mode:'polish'}),'已接受润色候选');
    $('toolAcceptMinorBtn').onclick=()=>withAction(()=>post('/api/chapter/candidate/accept',{chapter_no:Number($('toolChapter').value),mode:'minor'}),'已接受小幅重修候选');
    $('auditStartBtn').onclick=startAudit; $('auditStopBtn').onclick=()=>withAction(()=>post('/api/audit/stop'),'已发送审计停止请求'); $('auditViewBtn').onclick=viewAuditReport;
    $('auditRepairPlanBtn').onclick=createAuditRepairPlan;
    $('auditRepairRunBtn').onclick=startAuditRepair;
    $('auditRepairStopBtn').onclick=()=>withAction(()=>post('/api/audit/repair/stop'),'已发送修复停止请求');
    $('auditRepairRefreshBtn').onclick=()=>loadAuditRepairBatch(false);
    $('auditRepairCommitBtn').onclick=commitAuditRepair;
    $('auditRepairManualBtn').onclick=commitAuditRepairManual;
    $('auditRepairRollbackBtn').onclick=rollbackAuditRepair;
    $('exportMdBtn').onclick=()=>exportChapters('md'); $('exportTxtBtn').onclick=()=>exportChapters('txt'); $('exportZipBtn').onclick=()=>exportChapters('zip');
    $('readerReflowStartBtn').onclick=startReaderReflow;
    $('readerReflowStopBtn').onclick=()=>withAction(()=>post('/api/reader-reflow/stop'),'已发送智能分段停止请求');
    $('readerReflowViewBtn').onclick=viewReaderReflow;
    $('readerExportTxtBtn').onclick=()=>exportReaderReflow('txt'); $('readerExportMdBtn').onclick=()=>exportReaderReflow('md'); $('readerExportZipBtn').onclick=()=>exportReaderReflow('zip');
    $('mdManagerPasteBtn').onclick=mdManagerPaste; $('mdManagerParseBtn').onclick=mdManagerParse; $('mdManagerPreviewBtn').onclick=mdManagerPreview; $('mdManagerCommitBtn').onclick=mdManagerCommit; $('mdManagerClearBtn').onclick=()=>resetMdPreview(true);
    $('mdManagerInput').addEventListener('input',()=>{if(state.mdBeforeHash){state.mdBeforeHash=null;$('mdManagerCommitBtn').disabled=true;setPill('mdManagerPill','需要重新预览','warn');text('mdManagerNotice','文本已修改，请重新点击“检查修改”。');}});
    $('promptEditorDetails').ontoggle=()=>{if($('promptEditorDetails').open)loadPromptList(false);};
    $('promptReloadListBtn').onclick=()=>loadPromptList(true); $('promptReloadBtn').onclick=()=>loadPromptFile(); $('promptSaveBtn').onclick=savePromptFile;
    $('promptFileSelect').onchange=()=>loadPromptFile($('promptFileSelect').value);
    $('deepseekProviderSaveBtn').onclick=switchDeepSeekProvider;
    $('deepseekOfficialAccountSaveBtn').onclick=()=>switchPlatformAccount('official','deepseekOfficialAccount');
    $('volcAccountSaveBtn').onclick=()=>switchPlatformAccount('volcengine_agent_plan','volcAccount');
    $('deepseekBalanceRefreshBtn').onclick=refreshDeepSeekBalance;
    $('volcAfpRefreshBtn').onclick=refreshVolcAFP;
    $('deepseekOfficialKeySaveBtn').onclick=()=>savePlatformKey('official','deepseekOfficialKey','deepseekOfficialAccount');
    $('volcPlanKeySaveBtn').onclick=()=>savePlatformKey('volcengine_agent_plan','volcPlanKey','volcAccount');
    $('volcOpenApiKeySaveBtn').onclick=saveVolcOpenApiKeys;
    $('embedStartBtn').onclick=()=>withAction(()=>post('/api/embedding/start'),'Embedding 启动请求已发送');
    $('embedRestartBtn').onclick=()=>withAction(()=>post('/api/embedding/restart'),'Embedding 重启请求已发送');
    $('embedStopBtn').onclick=()=>withAction(()=>post('/api/embedding/stop'),'Embedding 停止请求已发送');
  }

  async function init(){
    bind(); const savedDraw=Number(localStorage.getItem('na_dlc_draw_count')||3); $('dlcDrawCount').value=String(Math.max(1,Math.min(20,Number.isFinite(savedDraw)?Math.round(savedDraw):3))); await Promise.all([refreshStatus(),loadConfig(),loadRouting(),loadDeepSeekConfig(),loadGrokConfig(),loadExportInfo(),loadReaderReflowInfo()]); await refreshProviderStatuses(); connectEvents();
    const current=state.status?.chapter||1; if(current){$('dlcChapter').value=Math.max(1,Number(current)-1||1);$('toolChapter').value=Number(current)||1;}
    setInterval(refreshStatus,1000); setInterval(()=>{ if(document.visibilityState==='visible')loadRouting(); },30000); setInterval(()=>{ if(document.visibilityState==='visible')refreshProviderStatuses(); },60000);
  }
  init();
})();
