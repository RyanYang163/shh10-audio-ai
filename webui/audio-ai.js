/* ============================================================================
   音频 AI 分析器 —— 前端逻辑
   ----------------------------------------------------------------------------
   依赖 ./app.js 提供的 API / U / UI / Jobs / Bars / Shell 与 ./icons.js 的 Icons。
   所有请求走 API.get/post（自动处理平台前缀与鉴权头），波形是自绘 canvas，
   不引任何第三方图表库。

   六个视图：概览 / 分析 / 字幕 / 媒体库 / 任务 / 设置
   ========================================================================== */

(function () {
  'use strict';

  const State = {
    engines: null,
    summary: null,
    settings: null,
    allowedRoots: [],
    analyze: { path: '', payload: null },
    subtitle: { path: '', data: null },
    library: { kind: 'audio', query: '', rows: [], total: 0 },
    convert: {
      target: 'srt', mode: 'subtitle', offset: 0, mergeShort: '',
      outputDir: '', roots: [], inputs: [],
    },
  };

  /* ------------------------------------------------------------ 小工具 */

  /**
   * 统一的卡片外壳。``flush`` 给表格用 —— ``.card.flush > .card-body`` 的内边距是 0，
   * 表格自己带单元格内边距，再套一层会让表格离边框很远。
   */
  function card(title, iconName, bodyNodes, actions, options) {
    const opts = options || {};
    const head = U.el('div', { class: 'card-head' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg(iconName || 'info', { size: 17 }) }),
        U.el('span', { text: title }),
      ]),
      actions ? U.el('div', { class: 'btn-row' }, actions) : null,
    ]);
    const body = U.el('div', { class: 'card-body',
      style: opts.flush ? null : 'padding:14px 16px' }, bodyNodes || []);
    return U.el('div', { class: 'card flush' }, [head, body]);
  }

  function tile(label, value, sub) {
    return U.el('div', { class: 'stat-tile' }, [
      U.el('div', { class: 'label', text: label }),
      U.el('div', { class: 'value' }, [
        U.el('span', { text: value }),
        sub ? U.el('small', { text: sub }) : null,
      ]),
    ]);
  }

  function fmtClock(seconds) {
    const total = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    if (h) return `${h}:${pad(m)}:${pad(s)}`;
    return `${m}:${s.toFixed(total < 10 ? 2 : 0).padStart(total < 10 ? 5 : 2, '0')}`;
  }

  function baseName(path) {
    if (!path) return '';
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
  }

  function dirName(path) {
    if (!path) return '';
    const index = String(path).replace(/[\\/]+$/, '').lastIndexOf('/');
    return index > 0 ? path.slice(0, index) : '';
  }

  /**
   * 带额外节点的横幅。
   *
   * ⚠️ ``UI.banner`` 只在 bodyHtml 为**真值**时才创建 ``.bd`` 容器，
   * 传空串进去就没有 ``.bd``，再 ``querySelector('.bd').appendChild(...)``
   * 会直接抛 null 异常（本应用「本地转写不可用」是默认状态，正好会踩到）。
   * 这里统一给一个空格，保证容器一定存在。
   */
  function bannerWith(kind, title, html, extraNodes) {
    const banner = UI.banner(kind, title, html || ' ');
    const body = banner.querySelector('.bd');
    (extraNodes || []).forEach((node) => { if (node) body.appendChild(node); });
    return banner;
  }

  function buttons(specs) {
    return specs.filter(Boolean).map((spec) => {
      const node = U.el('button', { class: 'btn ' + (spec.kind || 'ghost') }, [
        spec.icon ? U.el('span', { html: Icons.svg(spec.icon, { size: 13 }) }) : null,
        U.el('span', { text: spec.text }),
      ]);
      node.addEventListener('click', spec.onClick);
      return node;
    });
  }

  /** 目录选择：拿到路径后回调（走平台的 /api/fs 浏览，且必须已在白名单内） */
  function pickDir(title, start, onPick) {
    UI.pickDir({ title, start: start || '', onPick });
  }

  async function submitScan(paths) {
    if (!paths.length) { UI.warn(T('请先选择要扫描的目录')); return; }
    try {
      await Jobs.submit('scan', { roots: paths }, T('扫描音频与字幕'));
      UI.ok(T('扫描任务已提交'), T('可在底部任务栏查看进度'));
      if (window.ShellView) window.ShellView.show('jobs');
    } catch (error) { UI.err(error); }
  }

  /* ------------------------------------------------------------ 概览 */

  async function renderOverview(host) {
    host.innerHTML = '';
    host.appendChild(UI.banner('info', T('正在加载…'), ''));

    let summary;
    try {
      summary = await API.get('api/audio/summary');
      State.summary = summary;
    } catch (error) {
      host.innerHTML = '';
      host.appendChild(UI.banner('error', T('无法读取统计信息'),
        U.esc(error.message) + T('<ul><li>服务可能正在重启，稍后重试</li>') +
        T('<li>或查看日志：journalctl -u shh10-audio-ai</li></ul>')));
      return;
    }

    host.innerHTML = '';

    if (!State.allowedRoots.length) {
      const action = U.el('button', { class: 'btn primary', text: T('去设置可访问目录') });
      action.addEventListener('click', () => window.ShellView.show('settings'));
      host.appendChild(bannerWith('warn', T('还没有配置可访问目录'),
        T('本应用默认只读，且白名单初始为空 —— 必须由你指定它才能读哪些目录。'),
        [U.el('div', { class: 'mt1' }, [action])]));
    }

    host.appendChild(U.el('div', { class: 'grid cols-4 mb2' }, [
      tile(T('收录音频'), U.num(summary.audio_total), U.size(summary.total_bytes)),
      tile(T('总时长'), fmtClock(summary.duration),
        T('可分析 {n} 个', { n: U.num(summary.analyzable) })),
      tile(T('字幕文件'), U.num(summary.subtitle_total),
        summary.subtitle_failed ? T('{n} 个解析失败', { n: summary.subtitle_failed }) : T('全部解析正常')),
      tile(T('已转换'), U.num(summary.converted), T('输出在你自己指定的目录')),
    ]));

    const engines = State.engines || {};
    const engineCard = U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('cpu', { size: 17 }) }),
        U.el('span', { text: T('能力与可选引擎') }),
      ]),
      UI.banner('ok', T('离线能力全部可用（不需要任何外部程序）'),
        T('元数据读取（WAV / FLAC / MP3 / OGG / M4A）、WAV 波形与静音检测、') +
        T('LRC / SRT / VTT / TXT 互转 —— 全部由本应用用 Python 标准库实现。')),
    ]);
    const local = engines.local || {};
    const remote = engines.remote || {};
    if (engines.transcribe_available) {
      engineCard.appendChild(UI.banner('ok', T('语音转写可用'),
        (local.available ? T('本机已检测到转写命令：') + U.esc(local.detail || '')
          : T('本机没有转写命令，将使用你配置的远程接口：') + U.esc(remote.base_url || ''))));
    } else {
      const detail = U.el('div', {}, [
        U.el('div', { text: T('本地转写不可用 —— ') + (local.detail || T('未检测到引擎')) }),
        U.el('div', { class: 'mt1', text: T('这不影响其它任何功能。要让转写可用，可以：') }),
        U.el('ul', {}, [
          U.el('li', { text: T('在系统上安装 faster-whisper 命令行；或') }),
          U.el('li', { text: T('在「设置」里填写你自己可信任的 OpenAI 兼容接口并显式启用。') }),
        ]),
      ]);
      engineCard.appendChild(bannerWith('warn', T('本地转写不可用'), '', [detail]));
    }
    host.appendChild(engineCard);

    if (summary.formats && summary.formats.length) {
      const bars = U.el('div', {});
      Bars.render(bars, summary.formats.map((row, index) => ({
        label: row.format || T('未知'),
        value: row.seconds || row.n,
        text: T('{n} 个 · {time}', { n: U.num(row.n), time: fmtClock(row.seconds || 0) }),
        color: U.color(index, 48),
      })));
      host.appendChild(card(T('格式分布'), 'chart', [bars]));
    }

    const quick = U.el('div', { class: 'btn-row' }, []);
    const scanAudio = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('scan', { size: 14 }) }),
      U.el('span', { text: T('扫描目录') }),
    ]);
    scanAudio.addEventListener('click', () => {
      pickDir(T('选择要扫描的目录（音频 + 字幕）'), State.allowedRoots[0] || '',
        (path) => submitScan([path]));
    });
    quick.appendChild(scanAudio);
    host.appendChild(card(T('快速开始'), 'bolt', [
      U.el('div', { class: 'small muted mb1', text:
        T('扫描后会建立本地索引（按修改时间 + 大小增量更新），之后探测、分析、批量转换都会快很多。') }),
      quick,
    ]));
  }

  /* ------------------------------------------------------------ 分析 */

  async function renderAnalyze(host) {
    host.innerHTML = '';

    const pathBox = U.el('div', { class: 'path empty', text: T('尚未选择文件') });
    const row = U.el('div', { class: 'picker-row' }, [
      pathBox,
      ...buttons([
        { text: T('选择 WAV'), icon: 'folderOpen', kind: 'primary', onClick: () => {
            pickDir(T('选择要分析的音频文件'), dirName(State.analyze.path) || State.allowedRoots[0] || '',
              (path) => { State.analyze.path = path; pathBox.textContent = path;
                          pathBox.classList.remove('empty'); runAnalyze(false); });
          } },
        { text: T('重新分析'), icon: 'refresh', onClick: () => runAnalyze(true) },
      ]),
    ]);
    if (State.analyze.path) {
      pathBox.textContent = State.analyze.path;
      pathBox.classList.remove('empty');
    }

    const result = U.el('div', {});
    const picker = card(T('选择音频文件'), 'music', [
      U.el('div', { class: 'small muted mb1', text:
        T('波形与静音分析需要解码 PCM，因此目前只支持 WAV（其它格式请先转成 PCM WAV）。') +
        T('元数据读取与字幕转换支持全部格式。') }),
      row,
    ]);
    host.appendChild(picker);
    host.appendChild(result);

    async function runAnalyze(force) {
      if (!State.analyze.path) { UI.warn(T('请先选择文件')); return; }
      result.innerHTML = '';
      result.appendChild(UI.banner('info', T('正在分析…'), T('长文件会走任务队列')));

      let meta = null;
      try {
        meta = await API.get('api/audio/probe?path=' + encodeURIComponent(State.analyze.path));
      } catch (error) { /* 元数据失败不阻断分析 */ }

      try {
        const params = ['path=' + encodeURIComponent(State.analyze.path)];
        if (force) params.push('force=1');
        const settings = (State.settings && State.settings.settings) || {};
        if (settings.default_threshold_db != null) {
          params.push('threshold_db=' + settings.default_threshold_db);
        }
        if (settings.default_min_silence_ms != null) {
          params.push('min_silence_ms=' + settings.default_min_silence_ms);
        }
        const data = await API.get('api/audio/analyze?' + params.join('&'));
        State.analyze.payload = data;
        result.innerHTML = '';
        renderAnalysis(result, meta, data);
      } catch (error) {
        result.innerHTML = '';
        renderAnalysisError(result, error, meta);
      }
    }

    if (State.analyze.payload) {
      result.innerHTML = '';
      renderAnalysis(result, null, State.analyze.payload);
    }
  }

  function renderAnalysisError(host, error, meta) {
    // 判断分支读结构化错误码，不拿 message 里的中文措辞做正则 —— 后端消息会随界面语言
    // 变化，正则匹配在非中文界面下会静默失效（canQueue 永远 false）。
    const canQueue = (error && (error.code === 'duration_limit' || error.status === 409));
    const banner = bannerWith('error', T('未能完成分析'), U.esc(error.message || ''));
    if (canQueue) {
      const action = U.el('button', { class: 'btn primary', text: T('改用后台任务分析') });
      action.addEventListener('click', async () => {
        try {
          await Jobs.submit('analyze', { path: State.analyze.path },
            T('分析 {name}', { name: baseName(State.analyze.path) }));
          UI.ok(T('已提交后台分析任务'), T('完成后可在任务列表查看，结果会写入缓存'));
          window.ShellView.show('jobs');
        } catch (err) { UI.err(err); }
      });
      banner.querySelector('.bd').appendChild(U.el('div', { class: 'mt1' }, [action]));
    } else if (error && error.hint) {
      banner.querySelector('.bd').appendChild(U.el('div', { class: 'mt1', text: error.hint }));
    }
    host.appendChild(banner);
    if (meta && meta.ok) host.appendChild(metadataCard(meta));
  }

  function metadataCard(meta) {
    const rows = [
      [T('时长'), T('{clock}（{n} 秒）', { clock: fmtClock(meta.duration), n: meta.duration })],
      [T('采样率'), meta.sample_rate ? U.num(meta.sample_rate) + ' Hz' : '—'],
      [T('声道'), meta.channels ? T('{n} 声道', { n: meta.channels }) : '—'],
      [T('位深'), meta.bit_depth ? meta.bit_depth + ' bit' : '—'],
      [T('编码'), (meta.codec || '—') + (meta.lossless ? T('（无损）') : '')],
      [T('比特率'), meta.bitrate ? U.num(Math.round(meta.bitrate / 1000)) + ' kbps' : '—'],
      [T('文件大小'), U.size(meta.size)],
      [T('格式'), meta.format || '—'],
    ];
    if (meta.duration_estimated) rows.push([T('时长精度'), T('按平均码率估算（无 Xing 头）')]);
    const tags = meta.tags || {};
    const tagKeys = Object.keys(tags);
    if (tagKeys.length) {
      rows.push([T('标签'), tagKeys.map((key) => key + ': ' + tags[key]).join('　')]);
    }
    return card(T('元数据'), 'tag', [U.el('div', { class: 'kv' }, rows.map(([key, value]) =>
      U.el('div', {}, [U.el('div', { class: 'k', text: key }),
                       U.el('div', { class: 'v', text: String(value) })])))]);
  }

  function renderAnalysis(host, meta, data) {
    host.appendChild(metadataCard(Object.assign({}, meta || {}, {
      path: data.path, size: data.size, duration: data.duration,
      sample_rate: data.sample_rate, channels: data.channels,
      bit_depth: data.bit_depth, codec: data.codec, format: data.format,
      bitrate: (meta && meta.bitrate) || 0, tags: data.tags || {},
    })));

    const canvas = U.el('canvas', { width: 1200, height: 180 });
    const legend = U.el('div', { class: 'wave-legend' }, [
      U.el('span', {}, [U.el('i', { style: 'background:var(--app-accent)' }), U.el('span', { text: T('波形峰值') })]),
      U.el('span', {}, [U.el('i', { style: 'background:rgba(224,49,49,.28)' }), U.el('span', { text: T('静音段落') })]),
    ]);
    const actions = buttons([
      { text: T('导出分析 JSON'), icon: 'download', onClick: () => {
          const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
          const url = URL.createObjectURL(blob);
          const link = U.el('a', { href: url, download: baseName(data.path) + '.analysis.json' });
          document.body.appendChild(link); link.click(); link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 4000);
          UI.ok(T('已导出分析结果'), T('浏览器会把它保存到下载目录'));
        } },
    ]);
    host.appendChild(card(T('波形与静音'), 'activity', [
      U.el('div', { class: 'wave-wrap' }, [canvas]), legend,
      U.el('div', { class: 'small muted mt1', text:
        T('逐帧指标：帧长 {frame} ms、共 {frames} 帧、静音阈值 {threshold} dBFS、最短静音 {silence} ms',
          { frame: data.frame_ms, frames: U.num(data.frames),
            threshold: data.threshold_db, silence: data.min_silence_ms }) +
        (data.cached ? T('（本次来自缓存）') : T('（本次现算并已缓存）')) }),
    ], actions));

    // 画布尺寸等布局稳定后再画，否则宽度拿到 0
    setTimeout(() => drawWaveform(canvas, data), 30);

    const rows = (data.silences || []).map((item, index) => ({ ...item, index: index + 1 }));
    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: '#' }), U.el('th', { text: T('开始') }),
        U.el('th', { text: T('结束') }), U.el('th', { text: T('时长') }),
        U.el('th', { text: T('在波形中的位置') }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    rows.forEach((row) => {
      const bar = U.el('div', { style:
        `position:relative;height:8px;background:var(--surface-2);border-radius:4px;overflow:hidden` }, [
        U.el('i', { style: `position:absolute;left:${(row.start / data.duration * 100).toFixed(3)}%;` +
          `width:${Math.max(0.3, (row.duration / data.duration * 100)).toFixed(3)}%;` +
          'top:0;bottom:0;background:#e03131;opacity:.55' }),
      ]);
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { class: 'mono', text: String(row.index) }),
        U.el('td', { class: 'mono', text: row.start.toFixed(3) + ' s' }),
        U.el('td', { class: 'mono', text: row.end.toFixed(3) + ' s' }),
        U.el('td', { class: 'mono', text: row.duration.toFixed(3) + ' s' }),
        U.el('td', {}, [bar]),
      ]));
    });
    table.appendChild(tbody);

    const silenceBody = rows.length
      ? [table, U.el('div', { class: 'small muted mt1', text:
          T('静音合计 {silence} 秒，有声合计 {speech} 秒。边界会量化到帧长（{frame} ms），所以实测值通常比理论值短一个帧长。',
            { silence: data.silence_total, speech: data.speech_total, frame: data.frame_ms }) })]
      : [UI.empty('check', T('没有检测到静音段落'),
          T('整段音频都高于阈值，或者静音段都短于最短静音时长。可以放宽阈值再试。'))];
    host.appendChild(card(T('静音段落（{n}）', { n: data.silence_count }), 'clock', silenceBody,
      null, { flush: rows.length > 0 }));
  }

  function drawWaveform(canvas, data) {
    const peaks = data.peaks || [];
    const duration = Number(data.duration) || 0;
    if (!peaks.length || !duration) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 800;
    const height = 180;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);

    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue('--app-accent').trim() || '#6D5BD0';
    const mid = height / 2;

    // 静音区间底色：让「哪一段没声音」一眼可见
    (data.silences || []).forEach((item) => {
      const left = item.start / duration * width;
      const span = Math.max(1, (item.end - item.start) / duration * width);
      ctx.fillStyle = 'rgba(224,49,49,.14)';
      ctx.fillRect(left, 0, span, height);
    });

    ctx.strokeStyle = accent;
    ctx.lineWidth = Math.max(1, width / peaks.length * 0.9);
    peaks.forEach((peak, index) => {
      const x = (index + 0.5) / peaks.length * width;
      const top = mid - peak.max * (mid - 4);
      const bottom = mid - peak.min * (mid - 4);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, Math.max(bottom, top + 0.8));
      ctx.stroke();
    });

    ctx.strokeStyle = 'rgba(128,128,128,.45)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(width, mid);
    ctx.stroke();
  }

  /* ------------------------------------------------------------ 字幕 */

  async function renderSubtitles(host) {
    host.innerHTML = '';
    const pathBox = U.el('div', { class: 'path empty', text: T('尚未选择字幕文件') });
    if (State.subtitle.path) {
      pathBox.textContent = State.subtitle.path;
      pathBox.classList.remove('empty');
    }
    const preview = U.el('div', {});

    host.appendChild(card(T('选择字幕文件'), 'type', [
      U.el('div', { class: 'small muted mb1', text:
        T('支持 LRC / SRT / VTT / TXT，格式按内容自动识别（改错扩展名也能认）。') }),
      U.el('div', { class: 'picker-row' }, [
        pathBox,
        ...buttons([
          { text: T('选择文件'), icon: 'folderOpen', kind: 'primary', onClick: () => {
              pickDir(T('选择字幕文件'), dirName(State.subtitle.path) || State.allowedRoots[0] || '',
                (path) => { State.subtitle.path = path; pathBox.textContent = path;
                            pathBox.classList.remove('empty'); loadSubtitle(); });
            } },
        ]),
      ]),
    ]));
    host.appendChild(preview);
    host.appendChild(convertCard());

    async function loadSubtitle() {
      if (!State.subtitle.path) { UI.warn(T('请先选择文件')); return; }
      preview.innerHTML = '';
      preview.appendChild(UI.banner('info', T('正在解析…'), ''));
      try {
        const data = await API.get('api/audio/subtitles?limit=500&path=' +
          encodeURIComponent(State.subtitle.path));
        State.subtitle.data = data;
        State.convert.inputs = [State.subtitle.path];
        preview.innerHTML = '';
        preview.appendChild(cueCard(data));
      } catch (error) {
        preview.innerHTML = '';
        preview.appendChild(UI.banner('error', T('解析失败'), U.esc(error.message) +
          (error.hint ? '<div class="mt1">' + U.esc(error.hint) + '</div>' : '')));
      }
    }

    if (State.subtitle.data) {
      preview.innerHTML = '';
      preview.appendChild(cueCard(State.subtitle.data));
    }
  }

  function cueCard(data) {
    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: '#' }), U.el('th', { text: T('开始') }),
        U.el('th', { text: T('结束') }), U.el('th', { text: T('文本') }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    (data.cues || []).forEach((cue) => {
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { class: 'mono', text: String(cue.index) }),
        U.el('td', { class: 'mono', text: cue.start == null ? '—' : cue.start.toFixed(3) }),
        U.el('td', { class: 'mono', text: cue.end == null ? '—' : cue.end.toFixed(3) }),
        U.el('td', { text: cue.text }),
      ]));
    });
    table.appendChild(tbody);
    const meta = data.meta || {};
    const metaKeys = Object.keys(meta);
    return card(T('解析结果（{format} / {n} 条）', { format: data.format.toUpperCase(), n: data.cue_count }), 'list', [
      U.el('div', { class: 'small muted mb1', text:
        T('编码 {encoding} ｜ 总时长 {duration} 秒',
          { encoding: data.encoding || T('未知'), duration: data.duration }) +
        (metaKeys.length ? T(' ｜ 标签：') + metaKeys.map((k) => k + '=' + meta[k]).join('，') : '') +
        (data.truncated ? T('（预览只显示前 500 条）') : '') }),
      table,
    ]);
  }

  function convertCard() {
    const target = U.el('select', {}, ['lrc', 'srt', 'vtt', 'txt'].map((value) =>
      U.el('option', { value, text: value.toUpperCase() })));
    target.value = State.convert.target;

    const mode = U.el('select', {}, [
      U.el('option', { value: 'subtitle', text: T('字幕模式（保持原样）') }),
      U.el('option', { value: 'lyric', text: T('歌词模式（自动合并过短的行）') }),
    ]);
    mode.value = State.convert.mode;

    const offset = U.el('input', { type: 'number', step: '0.1', value: '0',
      placeholder: T('例如 -2.5') });
    const mergeShort = U.el('input', { type: 'number', step: '0.1', value: '',
      placeholder: T('留空 = 按模式默认') });
    const outputBox = U.el('div', { class: 'path empty', text: T('默认：应用自己的 data/output') });
    const rootsBox = U.el('div', { class: 'chips' });
    const scope = U.el('select', {}, [
      U.el('option', { value: 'current', text: T('当前选中的文件') }),
      U.el('option', { value: 'roots', text: T('批量：目录里的全部字幕') }),
    ]);
    scope.value = State.convert.roots.length ? 'roots' : 'current';

    function refreshChips() {
      rootsBox.innerHTML = '';
      if (!State.convert.roots.length) {
        rootsBox.appendChild(U.el('span', { class: 'small faint', text: T('尚未添加目录') }));
      }
      State.convert.roots.forEach((path, index) => {
        const chip = U.el('span', { class: 'chip' }, [
          U.el('span', { text: path }),
          U.el('button', { text: '×', title: T('移除') }),
        ]);
        chip.querySelector('button').addEventListener('click', () => {
          State.convert.roots.splice(index, 1);
          refreshChips();
        });
        rootsBox.appendChild(chip);
      });
    }
    refreshChips();

    const submit = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('wand', { size: 14 }) }),
      U.el('span', { text: T('开始转换') }),
    ]);
    submit.addEventListener('click', async () => {
      const payload = {
        format: target.value,
        mode: mode.value,
        offset: Number(offset.value) || 0,
        output_dir: State.convert.outputDir || '',
      };
      if (mergeShort.value !== '') payload.merge_short = Number(mergeShort.value);
      if (scope.value === 'roots') {
        if (!State.convert.roots.length) { UI.warn(T('请先添加要批量转换的目录')); return; }
        payload.roots = State.convert.roots.slice();
      } else {
        if (!State.subtitle.path) { UI.warn(T('请先选择一个字幕文件')); return; }
        payload.inputs = [State.subtitle.path];
      }
      try {
        const data = await API.post('api/audio/convert', payload);
        const count = (payload.inputs || []).length || T('整批');
        UI.ok(T('转换任务已提交'), T('任务 #{id}：{what}', { id: data.job.id, what: count }));
        Jobs.tick();
        window.ShellView.show('jobs');
      } catch (error) { UI.err(error); }
    });

    const body = [
      U.el('div', { class: 'form-grid' }, [
        U.el('div', {}, [U.el('label', { text: T('目标格式') }), target]),
        U.el('div', {}, [U.el('label', { text: T('转换模式') }), mode]),
        U.el('div', {}, [U.el('label', { text: T('时间轴平移（秒，可为负）') }), offset]),
        U.el('div', {}, [U.el('label', { text: T('合并阈值（秒，歌词模式默认 1.5）') }), mergeShort]),
        U.el('div', {}, [U.el('label', { text: T('范围') }), scope]),
      ]),
      U.el('div', { class: 'picker-row mt1' }, [
        outputBox,
        ...buttons([
          { text: T('选择输出目录'), icon: 'folderOpen', onClick: () => {
              pickDir(T('选择输出目录（必须在可访问目录内）'),
                dirName(State.subtitle.path) || State.allowedRoots[0] || '',
                (path) => { State.convert.outputDir = path; outputBox.textContent = path;
                            outputBox.classList.remove('empty'); });
            } },
          { text: T('清除'), onClick: () => {
              State.convert.outputDir = '';
              outputBox.textContent = T('默认：应用自己的 data/output');
              outputBox.classList.add('empty');
            } },
        ]),
      ]),
      U.el('div', { class: 'picker-row' }, [
        rootsBox,
        ...buttons([
          { text: T('添加目录'), icon: 'plus', onClick: () => {
              pickDir(T('选择要批量转换的目录'), State.allowedRoots[0] || '', (path) => {
                if (State.convert.roots.indexOf(path) < 0) State.convert.roots.push(path);
                scope.value = 'roots';
                refreshChips();
              });
            } },
        ]),
      ]),
      U.el('div', { class: 'small muted', text:
        T('输出文件写在你选择的目录里；同名时自动加 -1 / -2 后缀，绝不覆盖已有文件。') }),
      U.el('div', { class: 'mt1' }, [submit]),
    ];
    return card(T('转换为其它格式'), 'wand', body);
  }

  /* ------------------------------------------------------------ 媒体库 */

  async function renderLibrary(host) {
    host.innerHTML = '';
    const kind = U.el('select', {}, [
      U.el('option', { value: 'audio', text: T('音频文件') }),
      U.el('option', { value: 'subtitle', text: T('字幕文件') }),
      U.el('option', { value: 'all', text: T('全部') }),
    ]);
    kind.value = State.library.kind;
    const search = U.el('input', { type: 'search', placeholder: T('按路径过滤…'),
      value: State.library.query });
    const body = U.el('div', {});
    const info = U.el('span', { class: 'small muted' });

    async function load() {
      body.innerHTML = '';
      body.appendChild(UI.banner('info', T('正在读取…'), ''));
      try {
        const data = await API.get('api/audio/files?limit=200&kind=' + kind.value +
          '&q=' + encodeURIComponent(search.value.trim()));
        body.innerHTML = '';
        const rows = data.files || [];
        info.textContent = T('共 {n} 条', { n: U.num(data.total) }) +
          (data.total > rows.length ? T('（显示前 {n} 条）', { n: rows.length }) : '');
        if (!rows.length) {
          body.appendChild(UI.empty('inbox', T('还没有收录任何文件'),
            T('到「概览」或「设置」里扫描一个目录，收录后这里就能看到。')));
          return;
        }
        body.appendChild(libraryTable(rows, kind.value));
      } catch (error) {
        body.innerHTML = '';
        body.appendChild(UI.banner('error', T('读取失败'), U.esc(error.message)));
      }
    }

    function libraryTable(rows, view) {
      const audio = view !== 'subtitle';
      const columns = audio
        ? [T('文件'), T('格式'), T('时长'), T('采样率'), T('声道'), T('位深'), T('大小')]
        : [T('文件'), T('格式'), T('条目数'), T('总时长'), T('大小')];
      const table = U.el('table', { class: 'data' }, [
        U.el('thead', {}, [U.el('tr', {}, columns.map((text) => U.el('th', { text })))]),
      ]);
      const tbody = U.el('tbody', {});
      rows.forEach((row) => {
        const name = U.el('td', {}, [
          U.el('div', { text: baseName(row.path) }),
          U.el('div', { class: 'small faint', text: dirName(row.path) }),
          row.error ? U.el('div', { class: 'small', style: 'color:var(--danger)',
            text: row.error }) : null,
        ]);
        tbody.appendChild(U.el('tr', {}, audio ? [
          name,
          U.el('td', { text: (row.format || '—') + (row.codec ? ' / ' + row.codec : '') }),
          U.el('td', { class: 'mono', text: fmtClock(row.duration) }),
          U.el('td', { class: 'mono', text: row.sample_rate ? U.num(row.sample_rate) : '—' }),
          U.el('td', { class: 'mono', text: row.channels || '—' }),
          U.el('td', { class: 'mono', text: row.bit_depth || '—' }),
          U.el('td', { class: 'mono', text: U.size(row.size) }),
        ] : [
          name,
          U.el('td', { text: row.format || '—' }),
          U.el('td', { class: 'mono', text: U.num(row.cue_count) }),
          U.el('td', { class: 'mono', text: fmtClock(row.duration) }),
          U.el('td', { class: 'mono', text: U.size(row.size) }),
        ]));
      });
      table.appendChild(tbody);
      return table;
    }

    const exportCsv = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('download', { size: 13 }) }),
      U.el('span', { text: T('导出 CSV') })]);
    exportCsv.addEventListener('click', () => showExport('csv', kind.value));
    const exportJson = exportCsv.cloneNode(true);
    exportJson.querySelector('span:last-child').textContent = T('导出 JSON');
    exportJson.addEventListener('click', () => showExport('json', kind.value));

    kind.addEventListener('change', () => { State.library.kind = kind.value; load(); });
    search.addEventListener('input', U.debounce(() => { State.library.query = search.value; load(); }, 300));

    host.appendChild(card(T('已收录文件'), 'database', [
      U.el('div', { class: 'form-grid mb1' }, [
        U.el('div', {}, [U.el('label', { text: T('类型') }), kind]),
        U.el('div', {}, [U.el('label', { text: T('过滤') }), search]),
      ]),
      U.el('div', { class: 'btn-row mb1' }, [exportCsv, exportJson, info]),
      body,
    ]));
    await load();
  }

  async function showExport(fmt, kind) {
    try {
      if (fmt === 'csv') {
        const response = await fetch(API.url('api/audio/export?format=csv&kind=' + kind),
          { headers: API.authHeaders ? API.authHeaders() : {} });
        const text = await response.text();
        UI.modal({ title: T('导出 CSV'), icon: 'download', wide: true,
          bodyHtml: '<pre class="logview">' + U.esc(text) + '</pre>',
          buttons: [{ text: T('关闭') }, { text: T('复制全部'), kind: 'primary', onClick: (close) => {
            copyText(text); close(); } }] });
        return;
      }
      const data = await API.get('api/audio/export?format=json&kind=' + kind);
      const text = JSON.stringify(data, null, 2);
      UI.modal({ title: T('导出 JSON'), icon: 'download', wide: true,
        bodyHtml: '<pre class="logview">' + U.esc(text) + '</pre>',
        buttons: [{ text: T('关闭') }, { text: T('复制全部'), kind: 'primary', onClick: (close) => {
          copyText(text); close(); } }] });
    } catch (error) { UI.err(error); }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => UI.ok(T('已复制到剪贴板')),
        () => UI.warn(T('复制失败'), T('请手动选中文本复制')));
      return;
    }
    UI.warn(T('当前环境不支持自动复制'), T('请手动选中文本复制'));
  }

  /* ------------------------------------------------------------ 任务 */

  async function renderJobs(host) {
    host.innerHTML = '';
    const body = U.el('div', {});
    host.appendChild(card(T('任务'), 'activity', [body]));
    await reload();
    Jobs.reload = reload;

    async function reload() {
      try {
        const data = await API.get('api/jobs?limit=50');
        Jobs.renderTable(body, data.jobs || [], {
          emptyHint: T('扫描目录、批量转换、分析长音频、语音转写都会出现在这里'),
        });
      } catch (error) {
        body.innerHTML = '';
        body.appendChild(UI.banner('error', T('读取任务失败'), U.esc(error.message)));
      }
    }
  }

  /* ------------------------------------------------------------ 设置 */

  async function renderSettings(host) {
    host.innerHTML = '';
    const rootsBox = U.el('div', { class: 'chips mb1' });
    const settings = (State.settings && State.settings.settings) || {};

    function refreshRoots() {
      rootsBox.innerHTML = '';
      if (!State.allowedRoots.length) {
        rootsBox.appendChild(U.el('span', { class: 'small faint', text: T('白名单为空 —— 应用读不了任何目录') }));
      }
      State.allowedRoots.forEach((path, index) => {
        const chip = U.el('span', { class: 'chip' }, [
          U.el('span', { text: path }),
          U.el('button', { text: '×', title: T('移除') }),
        ]);
        chip.querySelector('button').addEventListener('click', async () => {
          const next = State.allowedRoots.slice();
          next.splice(index, 1);
          try {
            const data = await API.post('api/settings', { allowed_roots: next });
            State.allowedRoots = data.settings.allowed_roots || [];
            refreshRoots();
            UI.ok(T('已移除'));
          } catch (error) { UI.err(error); }
        });
        rootsBox.appendChild(chip);
      });
    }
    refreshRoots();

    const addRoot = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('plus', { size: 14 }) }),
      U.el('span', { text: T('添加目录') }),
    ]);
    addRoot.addEventListener('click', () => {
      pickDir(T('选择允许本应用读取的目录'), State.allowedRoots[0] || '', async (path) => {
        const next = State.allowedRoots.concat([path]);
        try {
          const data = await API.post('api/settings', { allowed_roots: next });
          State.allowedRoots = data.settings.allowed_roots || [];
          refreshRoots();
          UI.ok(T('已添加'), path);
        } catch (error) { UI.err(error); }
      });
    });

    const scanBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('scan', { size: 14 }) }),
      U.el('span', { text: T('扫描白名单全部目录') })]);
    scanBtn.addEventListener('click', () => submitScan(State.allowedRoots));

    // ---- 界面语言 ----
    // 放在设置页最前：非中文用户进来第一眼就该看到它。
    // UI.langSelect 里已经处理了「落 localStorage + 套用 + 同步到后端 settings.ui_language」。
    host.appendChild(card(T('界面语言'), 'globe', [
      U.el('div', { class: 'small muted mb1', text:
        T('选择本应用界面的语言。首次打开时会跟随浏览器语言。') }),
      UI.langSelect(),
    ]));

    host.appendChild(card(T('可访问目录'), 'folderOpen', [
      U.el('div', { class: 'small muted mb1', text:
        T('应用默认只读，且初始白名单为空。所有读写路径都会先做 realpath 校验并比对白名单，') +
        T('目录穿越与指向白名单外的软链都会被拒绝。') }),
      rootsBox,
      U.el('div', { class: 'btn-row' }, [addRoot, scanBtn]),
    ]));

    // ---- 分析默认参数 ----
    const frameMs = U.el('input', { type: 'number', value: String(settings.default_frame_ms ?? 20) });
    const threshold = U.el('input', { type: 'number', value: String(settings.default_threshold_db ?? -45) });
    const minSilence = U.el('input', { type: 'number', value: String(settings.default_min_silence_ms ?? 300) });
    const syncSeconds = U.el('input', { type: 'number', value: String(settings.analyze_sync_seconds ?? 120) });
    const saveAnalyze = U.el('button', { class: 'btn primary', text: T('保存分析参数') });
    saveAnalyze.addEventListener('click', async () => {
      try {
        await API.post('api/settings', {
          default_frame_ms: Number(frameMs.value) || 20,
          default_threshold_db: Number(threshold.value),
          default_min_silence_ms: Number(minSilence.value),
          analyze_sync_seconds: Number(syncSeconds.value) || 120,
        });
        await refreshSettings();
        UI.ok(T('已保存分析参数'));
      } catch (error) { UI.err(error); }
    });
    host.appendChild(card(T('分析默认参数'), 'activity', [
      U.el('div', { class: 'form-grid' }, [
        U.el('div', {}, [U.el('label', { text: T('帧长（毫秒）') }), frameMs]),
        U.el('div', {}, [U.el('label', { text: T('静音阈值（dBFS）') }), threshold]),
        U.el('div', {}, [U.el('label', { text: T('最短静音（毫秒）') }), minSilence]),
        U.el('div', {}, [U.el('label', { text: T('同步分析上限（秒）') }), syncSeconds]),
      ]),
      U.el('div', { class: 'small muted mt1', text:
        T('超过「同步分析上限」的音频会要求改用后台任务，避免一个请求被占住很久。') }),
      U.el('div', { class: 'mt1' }, [saveAnalyze]),
    ]));

    host.appendChild(remoteEngineCard());
  }

  function remoteEngineCard() {
    const settings = (State.settings && State.settings.settings) || {};
    const remote = (State.engines && State.engines.remote) || {};
    const enabled = U.el('select', {}, [
      U.el('option', { value: 'off', text: T('关闭（默认）') }),
      U.el('option', { value: 'on', text: T('启用') }),
    ]);
    enabled.value = remote.enabled ? 'on' : 'off';
    const baseUrl = U.el('input', { type: 'text', value: settings.remote_base_url || '',
      placeholder: 'https://example.com' });
    const model = U.el('input', { type: 'text', value: settings.remote_model || 'whisper-1' });
    const language = U.el('input', { type: 'text', value: settings.remote_language || '',
      placeholder: T('留空 = 自动识别，例如 zh / en') });
    const apiKey = U.el('input', { type: 'password', value: '',
      placeholder: remote.has_key ? T('已保存（留空则不修改）') : T('粘贴你的 API Key') });

    const save = U.el('button', { class: 'btn primary', text: T('保存远程转写设置') });
    save.addEventListener('click', async () => {
      const payload = {
        remote: {
          enabled: enabled.value === 'on',
          base_url: baseUrl.value.trim(),
          model: model.value.trim() || 'whisper-1',
          language: language.value.trim(),
        },
      };
      if (apiKey.value) payload.api_key = apiKey.value;
      try {
        await API.post('api/audio/engines', payload);
        await refreshSettings();
        apiKey.value = '';
        UI.ok(T('已保存远程转写设置'));
      } catch (error) {
        if (payload.remote.enabled && error.code === 'remote_confirm_required') {
          const ok = await UI.confirm({
            title: T('确认启用远程转写？'),
            body: T('启用后，你选择的音频会被上传到下面这个地址做转写：\n\n') +
                  (baseUrl.value.trim() || T('(未填写)')) +
                  T('\n\n音频内容会离开你的 NAS。请确认这是你自己可信任的接口。'),
            confirmText: T('我确认，启用'),
            danger: true,
          });
          if (!ok) return;
          payload.confirm = true;
          try {
            await API.post('api/audio/engines', payload);
            await refreshSettings();
            apiKey.value = '';
            UI.ok(T('已启用远程转写'), baseUrl.value.trim());
          } catch (err2) { UI.err(err2); }
          return;
        }
        UI.err(error);
      }
    });

    const clear = U.el('button', { class: 'btn', text: T('清除已保存的 API Key') });
    clear.addEventListener('click', async () => {
      const ok = await UI.confirm({
        title: T('清除 API Key？'), body: T('清除后远程转写将不可用，直到你重新填写。'),
        danger: true, requireText: '清除',
      });
      if (!ok) return;
      try {
        await API.post('api/audio/engines', { clear_key: true, remote: { enabled: false } });
        await refreshSettings();
        UI.ok(T('已清除'));
      } catch (error) { UI.err(error); }
    });

    return card(T('远程语音转写（可选，默认关闭）'), 'key', [
      UI.banner(remote.has_key ? 'info' : 'warn',
        remote.ready ? T('远程转写已就绪') : T('远程转写未启用'),
        T('核心功能完全离线、不产生任何出网请求。只有在这里显式填写地址、API Key 并启用后，') +
        T('转写才会把音频发到你填的那个接口。API Key 单独保存在 data/config/secrets.json') +
        T('（权限 600），接口响应与日志里都不会回显它。')),
      U.el('div', { class: 'form-grid mt1' }, [
        U.el('div', {}, [U.el('label', { text: T('状态') }), enabled]),
        U.el('div', {}, [U.el('label', { text: T('接口地址（OpenAI 兼容）') }), baseUrl]),
        U.el('div', {}, [U.el('label', { text: T('模型') }), model]),
        U.el('div', {}, [U.el('label', { text: T('语言（可选）') }), language]),
        U.el('div', {}, [U.el('label', { text: 'API Key' }), apiKey]),
      ]),
      U.el('div', { class: 'btn-row mt1' }, [save, clear]),
    ]);
  }

  async function refreshSettings() {
    const data = await API.get('api/settings');
    State.settings = data;
    State.allowedRoots = (data.settings && data.settings.allowed_roots) || [];
    State.engines = await API.get('api/audio/engines');
    updateEngineMeta();
  }

  function updateEngineMeta() {
    const engines = State.engines || {};
    const node = U.byId('engine-meta');
    if (!node) return;
    node.textContent = engines.transcribe_available
      ? T('离线分析 + 转写可用')
      : T('离线分析可用（本地转写不可用）');
  }

  /* ------------------------------------------------------------ 启动 */

  async function boot() {
    Jobs.mountTaskbar(U.byId('taskbar'));
    Jobs.start(2500);

    const shell = Shell.init({
      overview: { label: T('概览'), icon: 'home', render: renderOverview },
      analyze: { label: T('波形分析'), icon: 'activity', render: renderAnalyze },
      subtitles: { label: T('字幕转换'), icon: 'type', render: renderSubtitles },
      library: { label: T('媒体库'), icon: 'list', render: renderLibrary },
      jobs: { label: T('任务'), icon: 'clock', render: renderJobs },
      settings: { label: T('设置'), icon: 'settings', render: renderSettings },
    }, { defaultView: 'overview' });
    window.ShellView = shell;
    // 切语言后重渲染当前视图 —— 框架只负责换静态文案，动态渲染的部分要靠这个事件
    Shell.bindLanguage(shell);

    try {
      await refreshSettings();
      await Shell.loadAppInfo();
    } catch (error) {
      const node = U.byId('engine-meta');
      if (node) node.textContent = T('服务未就绪');
    }

    shell.show(shell.current() || 'overview');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
