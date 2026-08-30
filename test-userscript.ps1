$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'newtoki-dark-reader.user.js'
$source = Get-Content -Raw -LiteralPath $scriptPath

node --check $scriptPath
if ($LASTEXITCODE -ne 0) { throw 'JavaScript syntax check failed.' }
if ($source.Contains("addEventListener('keydown'")) { throw 'Arrow-key navigation remains.' }
if ($source.Contains('nextFired')) { throw 'Automatic next-episode navigation remains.' }
if ($source.Contains('nt-wait-overlay')) { throw 'Blocking wait overlay remains.' }
if (-not $source.Contains('if (atBottom)')) { throw 'Bottom navigation reveal is missing.' }
if ($source.Contains('if (!holders.length) return true')) { throw 'Mobile webtoon can bypass image readiness.' }
if (-not $source.Contains('e.stopImmediatePropagation()')) { throw 'Viewer navigation can still be intercepted by the site router.' }
if ($source.Contains('a.href = href;')) { throw 'Viewer navigation still exposes links to the site router.' }
if ($source.Contains('if (!bodyEl) return;')) { throw 'Missing async body container still aborts viewer startup.' }
if (-not $source.Contains('if (!el) return false;')) { throw 'Body readiness does not wait for the container.' }
if (-not $source.Contains('el.shadowRoot.textContent')) { throw 'Novel readiness ignores Shadow DOM content.' }
if (-not $source.Contains("addEventListener('novel-content-ready'")) { throw 'Novel completion event is not observed.' }
if (-not $source.Contains('// @updateURL    https://raw.githubusercontent.com/yuisatomi/newtoki-dark-reader/main/newtoki-dark-reader.user.js')) { throw 'Automatic update URL is missing.' }
if (-not $source.Contains('id="nt-warm"')) { throw 'Warm-tone control is missing.' }
if (-not $source.Contains('data-theme="dark"') -or -not $source.Contains('data-theme="paper"')) { throw 'Reader theme presets are missing.' }
if (-not $source.Contains("cfg.theme = cfg.theme === 'paper' ? 'paper' : 'dark'")) { throw 'Saved reader theme is not validated.' }
if (-not $source.Contains('#nt-dark-nav .primary')) { throw 'Primary button theme does not cover button elements.' }
if ($source.Contains('#nt-dark-nav a.primary')) { throw 'Primary theme is still restricted to link elements.' }
if (-not $source.Contains('accent-color:var(--nt-primary')) { throw 'Range controls do not follow the selected theme.' }
if (-not $source.Contains("document.documentElement.style.setProperty('--nt-title', title)")) { throw 'Controls outside the reader do not inherit the theme text color.' }
if (-not $source.Contains("bodyEl.style.setProperty('--theme-novel-text-color', text)")) { throw 'Shadow DOM text color is not warmed.' }
if ($source.Contains('let cfg = DEFAULTS;')) { throw 'Settings mutate their defaults.' }
if (-not $source.Contains('GM_getValue(READER_CFG_KEY, null)') -or -not $source.Contains('GM_setValue(READER_CFG_KEY, cfg)')) { throw 'Reader settings are not shared across domains.' }
if (-not $source.Contains('localStorage.getItem(READER_CFG_KEY)') -or -not $source.Contains('localStorage.setItem(READER_CFG_KEY')) { throw 'Reader settings migration is not rollback-safe.' }
if (-not $source.Contains('cfg.font = Math.max(12') -or -not $source.Contains('cfg.width = Math.max(360') -or -not $source.Contains('cfg.lh = Math.max(1.4')) { throw 'Saved numeric settings are not validated.' }
if (-not $source.Contains('// @grant        GM_xmlhttpRequest') -or -not $source.Contains('// @connect      reader-sync.flolim.com')) { throw 'Sync API permission is missing.' }
if (-not $source.Contains("const SYNC_URL = 'https://reader-sync.flolim.com'")) { throw 'Default sync server is incorrect.' }
if (-not $source.Contains("GM_getValue(SYNC_TOKEN_KEY, '')") -or -not $source.Contains('GM_setValue(SYNC_TOKEN_KEY, token)')) { throw 'Per-install sync token storage is missing.' }
if (-not $source.Contains("syncRequest('PUT', '/v1/progress'") -or -not $source.Contains("'/v1/progress?kind='")) { throw 'Progress sync API calls are missing.' }
if (-not $source.Contains("window.addEventListener('pagehide'")) { throw 'Final progress save is missing.' }
if (-not $source.Contains('remote.episode_id === episodeInfo.episodeId')) { throw 'Remote episode position is not restored.' }
if (-not $source.Contains("document.querySelector('.page-desc')?.textContent || titleText")) { throw 'Original episode metadata is not captured.' }
if (-not $source.Contains('episodeText.match(/(\d+)\s*화/)')) { throw 'Episode number is still restricted to the viewer title.' }
if (-not $source.Contains("document.querySelector('.theme-episode-pager')")) { throw 'Episode pagination is missing from list mode.' }
if (-not $source.Contains("url.searchParams.set('ntlist', '1')")) { throw 'Episode pagination leaves list mode.' }
if (-not $source.Contains(':not(.wr-subject):not(.wr-num)')) { throw 'Episode numbers are hidden in list mode.' }
if (-not $source.Contains("url.searchParams.set('ntep', episodeInfo.episodeId)")) { throw 'Current episode is not passed to list mode.' }
if (-not $source.Contains("targetItem.scrollIntoView({ block: 'center' })")) { throw 'Current episode is not centered in list mode.' }
if (-not $source.Contains("const READ_LIBRARY_KEY = 'ntReaderLibrary'")) { throw 'Read library storage is missing.' }
if (-not $source.Contains('injectLibraryButton(isEpisodePage)')) { throw 'Site-wide read library access is missing.' }
if (-not $source.Contains("library.addEventListener('click', openReadLibrary)")) { throw 'Viewer navigation cannot open the read library.' }
if (-not $source.Contains('saveReadLibrary(getReadLibrary().filter')) { throw 'Read library entries cannot be deleted.' }
if (-not $source.Contains("open.textContent = '이어보기'")) { throw 'Read library entries cannot be resumed.' }
if (-not $source.Contains('async function refreshReadMetadata')) { throw 'Legacy read metadata cannot be recovered.' }
if (-not $source.Contains('doc.querySelector(''meta[name="description"]'')')) { throw 'Episode metadata description is not parsed.' }
if (-not $source.Contains('metadataQueue = metadataQueue.then')) { throw 'Legacy metadata requests are not rate-limited.' }
if (-not $source.Contains("manualSaveButton.addEventListener('click'")) { throw 'Manual progress save control is missing.' }
if (-not $source.Contains("localStorage.setItem('ntRead:' + episodeInfo.kind")) { throw 'Manual save does not store the current episode.' }
if (-not $source.Contains('getSyncToken() ? saveRemoteProgress(ratio)')) { throw 'Manual save does not sync the current position.' }
$buildCall = $source.IndexOf('    buildViewer();')
$readWrite = $source.IndexOf("localStorage.setItem('ntRead:")
if ($buildCall -lt 0 -or $readWrite -lt $buildCall) { throw 'Episode is marked read before the viewer succeeds.' }
if (-not $source.Contains("e.target.closest('a, button, input, select, textarea, label")) { throw 'Edge navigation still captures interactive elements.' }
if (-not $source.Contains('window.getSelection()?.toString()')) { throw 'Edge navigation still captures text selection.' }

Write-Host 'userscript checks passed'
