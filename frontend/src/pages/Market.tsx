/**
 * 行情页 — 仿东方财富 A 股全市场实时行情列表。
 *
 * 数据源: /api/screener/market-snapshot (同花顺全市场实时快照 → enriched 缓存)。
 * 功能: 板块分页签(全部/沪A/深A/创业板/科创/北交所) + 关键词搜索 + 可排序虚拟化表格
 *       (代码/名称/最新价/涨跌幅/涨跌额/今开/昨收/最高/最低/成交量/成交额/振幅/换手率/量比/总市值)
 *       + 自动轮询(默认 10s) + 手动刷新。
 * 交互: 左键点击行 / 键盘回车 → 跳转个股分析页; 右键行 → 弹出"加入自选"菜单。
 * 口径: change_pct / amplitude 为小数制(0.0366=3.66%), turnover_rate 为百分数值(3.66=3.66%),
 *       market_cap 为元。
 */
import { useMemo, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Search, RefreshCw, BarChart3, Plus } from 'lucide-react'
import { api, type MarketSnapshotRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPrice, fmtPct, fmtBigNum, fmtVolume, priceColorClass } from '@/lib/format'
import { useTableSort } from '@/components/stock-table/useTableSort'
import { StockDataTable } from '@/components/stock-table/StockDataTable'
import type { ColumnConfig } from '@/lib/list-columns'
import { cn } from '@/lib/cn'
import { toast } from '@/components/Toast'

// ===== 板块分页签 =====
const MARKET_TABS = [
  { id: 'all', label: '全部' },
  { id: 'sh', label: '沪A' },
  { id: 'sz', label: '深A' },
  { id: 'cyb', label: '创业板' },
  { id: 'kc', label: '科创' },
  { id: 'bj', label: '北交所' },
] as const

type MarketTab = (typeof MARKET_TABS)[number]['id']

function tabOfSymbol(symbol: string): MarketTab | 'other' {
  const code = symbol.split('.')[0]
  // 注意: 更具体的市场代码必须在前判断(30x/68x), 否则会被 3/6 前缀吞掉
  if (code.startsWith('30')) return 'cyb'  // 创业板 300/301/302
  if (code.startsWith('68')) return 'kc'   // 科创板 688/689
  if (code.startsWith('6')) return 'sh'    // 沪A 600/601/603/605
  if (code.startsWith('0') || code.startsWith('3')) return 'sz' // 深A 000/001/002/003
  if (code.startsWith('4') || code.startsWith('8') || code.startsWith('9')) return 'bj'
  return 'other'
}

// ===== 列配置 =====
function col(id: string, label: string, align: ColumnConfig['align'] = 'right'): ColumnConfig {
  return { id, label, visible: true, align, source: { type: 'builtin', key: id } }
}

const COLUMNS: ColumnConfig[] = [
  col('symbol', '代码', 'left'),
  col('name', '名称', 'left'),
  col('close', '最新价'),
  col('change_pct', '涨跌幅'),
  col('change_amount', '涨跌额'),
  col('open', '今开'),
  col('prev_close', '昨收'),
  col('high', '最高'),
  col('low', '最低'),
  col('volume', '成交量'),
  col('amount', '成交额'),
  col('amplitude', '振幅'),
  col('turnover_rate', '换手率'),
  col('vol_ratio_5d', '量比'),
  col('market_cap', '总市值'),
]

function getSortValue(r: MarketSnapshotRow, c: ColumnConfig): any {
  return (r as any)[c.source.type === 'builtin' ? c.source.key : c.id]
}

function tdCls(align?: ColumnConfig['align']): string {
  if (align === 'left') return 'px-3 py-1.5 text-left'
  if (align === 'center') return 'px-3 py-1.5 text-center'
  return 'px-3 py-1.5 text-right tabular-nums'
}

/** 右键菜单状态: 位置 + 目标股票 */
interface CtxMenuState {
  x: number
  y: number
  symbol: string
  name: string
}

export function Market() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [tab, setTab] = useState<MarketTab>('all')
  const [query, setQuery] = useState('')
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const snapshot = useQuery({
    queryKey: QK.marketSnapshot,
    queryFn: api.marketSnapshot,
    // 自动轮询: 交易时段内前端定期刷新(实时模式时后台同花顺轮询也会写缓存)
    refetchInterval: 10_000,
  })

  const { sort, toggle, sortRows } = useTableSort<MarketSnapshotRow>(getSortValue)
  const rows = snapshot.data?.rows ?? []
  const asOf = snapshot.data?.as_of ?? null

  const filtered = useMemo(() => {
    const kw = query.trim().toLowerCase()
    let out = rows
    if (tab !== 'all') out = out.filter(r => tabOfSymbol(r.symbol) === tab)
    if (kw) {
      out = out.filter(r =>
        r.symbol.toLowerCase().includes(kw) ||
        (r.name ?? '').toLowerCase().includes(kw) ||
        (r.name ?? '').toLowerCase().includes(kw.replace(/^sh|^sz|^bj/, ''))
      )
    }
    return out
  }, [rows, tab, query])

  const sorted = useMemo(() => sortRows(filtered, COLUMNS), [filtered, sortRows])

  // 顶部统计
  const stat = useMemo(() => {
    let up = 0, down = 0, flat = 0, totalAmt = 0
    for (const r of rows) {
      const p = r.change_pct
      if (p == null || Number.isNaN(p)) continue
      if (p > 0) up++
      else if (p < 0) down++
      else flat++
      totalAmt += r.amount ?? 0
    }
    return { up, down, flat, totalAmt }
  }, [rows])

  // 行交互: 左键/回车 → 个股分析页
  const openStockAnalysis = (r: MarketSnapshotRow) => {
    const q = new URLSearchParams({ symbol: r.symbol, name: r.name ?? r.symbol })
    navigate(`/stock-analysis?${q.toString()}`)
  }

  // 行交互: 右键 → 自选股添加菜单
  const onRowContextMenu = (r: MarketSnapshotRow, e: React.MouseEvent) => {
    e.preventDefault()
    setCtxMenu({ x: e.clientX, y: e.clientY, symbol: r.symbol, name: r.name ?? r.symbol })
  }

  // 点击菜单外部关闭
  const dismissMenu = () => setCtxMenu(null)

  const addWatch = useMutation({
    mutationFn: (symbol: string) => api.watchlistAdd(symbol),
    onSuccess: (data) => {
      qc.setQueryData(QK.watchlist, data)
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
      toast('已加入自选', 'success')
    },
    onError: () => toast('加入自选失败', 'error'),
    onSettled: dismissMenu,
  })

  return (
    <div className="p-5 space-y-4 max-w-[1600px]">
      {/* 顶部: 标题 + 统计 + 刷新 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-secondary" />
          <h1 className="text-base font-semibold text-foreground">行情</h1>
          {asOf && <span className="text-[10px] text-muted/50 font-mono">{asOf}</span>}
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span className="text-bull">涨 {stat.up}</span>
          <span className="text-bear">跌 {stat.down}</span>
          <span className="text-muted">平 {stat.flat}</span>
          <span className="text-muted/60">成交 {fmtBigNum(stat.totalAmt)}</span>
          <span className="text-muted/40">共 {rows.length} 只</span>
        </div>
        <div className="flex-1" />
        <button
          onClick={() => snapshot.refetch()}
          disabled={snapshot.isFetching}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn text-xs text-muted hover:text-foreground hover:bg-elevated transition-colors disabled:opacity-50"
          title="手动刷新"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${snapshot.isFetching ? 'animate-spin' : ''}`} />
          刷新
        </button>
      </div>

      {/* 板块分页签 + 搜索 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 rounded-lg bg-elevated/40 p-1">
          {MARKET_TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={cn(
                'px-3 py-1.5 rounded-md text-xs transition-colors',
                tab === t.id
                  ? 'bg-accent text-white font-medium'
                  : 'text-muted hover:text-foreground',
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="relative ml-auto w-56">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted/50" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="代码 / 名称搜索"
            className="w-full h-9 pl-8 pr-3 rounded-lg bg-base border-0 ring-1 ring-border/40 text-xs text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </div>
      </div>

      {/* 行情表: 固定高度滚动容器 → 出现可拖动的垂直滚动条, 虚拟化跟随该容器滚动 */}
      <div className="rounded-card border border-border bg-surface/30 overflow-hidden">
        <div className="h-[calc(100vh-260px)] min-h-[320px] overflow-auto scrollbar-gutter-stable">
          <StockDataTable
            columns={COLUMNS}
            rows={sorted}
            sort={sort}
            onSortToggle={toggle}
            headerSticky
            minWidth={1500}
            className="border-0 rounded-none"
            rowClassName={() => 'border-t border-border hover:bg-elevated/50 cursor-pointer'}
            onRowClick={(r) => openStockAnalysis(r)}
            onRowContextMenu={(r, e) => onRowContextMenu(r, e)}
            renderCell={(r, c) => {
              const k = c.source.type === 'builtin' ? c.source.key : c.id
              const v = (r as any)[k]
              const align = c.align
              const base = tdCls(align)
              switch (k) {
                case 'symbol':
                  return <td className={base + ' text-muted font-mono'}>{v}</td>
                case 'name':
                  return <td className={base + ' font-medium text-foreground'}>{v ?? '—'}</td>
                case 'close':
                  return <td className={base + ' font-semibold ' + priceColorClass(r.change_pct)}>{fmtPrice(v)}</td>
                case 'change_pct':
                  return (
                    <td className={base}>
                      <span className={cn(
                        'inline-flex items-center px-1.5 py-0.5 rounded min-w-[64px] justify-center text-xs font-medium',
                        (r.change_pct ?? 0) > 0 ? 'bg-bull/12 text-bull'
                          : (r.change_pct ?? 0) < 0 ? 'bg-bear/12 text-bear'
                            : 'bg-elevated text-secondary',
                      )}>
                        {fmtPct(v)}
                      </span>
                    </td>
                  )
                case 'change_amount':
                  return <td className={base + ' ' + priceColorClass(v)}>{v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2)}</td>
                case 'open':
                case 'prev_close':
                case 'high':
                case 'low':
                  return <td className={base + ' text-muted'}>{fmtPrice(v)}</td>
                case 'volume':
                  return <td className={base + ' text-muted'}>{fmtVolume(v)}</td>
                case 'amount':
                  return <td className={base + ' text-muted'}>{fmtBigNum(v)}</td>
                case 'amplitude':
                  return <td className={base + ' text-muted'}>{fmtPct(v)}</td>
                case 'turnover_rate':
                  // enriched 换手率为百分数值(3.66=3.66%)
                  return <td className={base + ' text-muted'}>{v == null ? '—' : `${v.toFixed(2)}%`}</td>
                case 'vol_ratio_5d':
                  return <td className={base + ' text-muted'}>{v == null ? '—' : v.toFixed(2)}</td>
                case 'market_cap':
                  return <td className={base + ' text-muted'}>{fmtBigNum(v)}</td>
                default:
                  return <td className={base}>{v == null ? '—' : String(v)}</td>
              }
            }}
          />
        </div>
      </div>

      {/* 行情概要提示 */}
      <div className="text-[10px] text-muted/40 flex flex-wrap gap-x-4 gap-y-1">
        <span>数据来自同花顺全市场实时快照，交易时段每 10 秒自动刷新</span>
        <span>左键/回车打开个股分析，右键加入自选</span>
        <span>点击表头排序，输入代码/名称筛选</span>
      </div>

      {/* 右键菜单: 加入自选 */}
      {ctxMenu && (
        <>
          {/* 遮罩: 点击任意处关闭 */}
          <div className="fixed inset-0 z-40" onClick={dismissMenu} onContextMenu={(e) => { e.preventDefault(); dismissMenu() }} />
          <div
            ref={menuRef}
            className="fixed z-50 min-w-44 rounded-lg border border-border bg-surface shadow-xl p-1"
            style={{ left: ctxMenu.x, top: ctxMenu.y }}
          >
            <div className="px-3 py-1.5 border-b border-border/40 mb-1">
              <div className="text-xs font-medium text-foreground truncate">{ctxMenu.name}</div>
              <div className="text-[10px] font-mono text-muted">{ctxMenu.symbol}</div>
            </div>
            <button
              onClick={() => addWatch.mutate(ctxMenu.symbol)}
              disabled={addWatch.isPending}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-xs text-foreground/90 hover:bg-elevated transition-colors disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5 text-accent" />
              {addWatch.isPending ? '添加中…' : '加入自选股'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

export default Market