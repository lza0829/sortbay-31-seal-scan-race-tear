import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Scans() {
  const [rows, setRows] = useState([])
  const [shift, setShift] = useState(null)
  const [barcode, setBarcode] = useState('')
  const [dest, setDest] = useState('110')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const load = async () => {
    // 装载入口与封班状态同刷：扫描册、待路由桶、班次灯取自同一落账
    const [scans, rec] = await Promise.all([api('/扫描'), api('/班次/对账').catch(() => null)])
    setRows(scans)
    setShift(rec ? rec.班次 : null)
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const sealed = !!shift?.sealed
  const create = async () => {
    setErr(''); setMsg('')
    try {
      await api('/扫描', { method: 'POST', body: JSON.stringify({ 条码: barcode, 目的地码: dest }) })
      setMsg('过机装载成功'); setBarcode('')
    } catch (e) { setErr(e.message) }
    finally { load().catch((e) => setErr(e.message)) }
  }
  const routeOne = async (id) => {
    setErr(''); setMsg('')
    try {
      await api(`/扫描/${id}/路由`, { method: 'POST' })
      setMsg('路由成功')
    } catch (e) { setErr(e.message) }
    finally { load().catch((e) => setErr(e.message)) }
  }
  return (
    <div className="panel">
      <h2>过机扫描</h2>
      <p className="muted">
        当前班次：{shift ? `${shift.business_day} ${shift.name}` : '无'} · {sealed ? '已封班（装载入口已关）' : '未封班'}
      </p>
      <div className="row">
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} /></label>
        <label>目的地码<input value={dest} onChange={(e) => setDest(e.target.value)} /></label>
        <button type="button" onClick={create} disabled={sealed}>装载扫描</button>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <table>
        <thead><tr><th>编号</th><th>条码</th><th>目的地</th><th>状态</th><th>口门</th><th>操作</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td><td>{r.barcode}</td><td>{r.dest_code}</td><td>{r.status}</td><td>{r.chute_id || '-'}</td>
              <td>{!sealed && (r.status === '待路由' || r.status === '错分回流') && <button type="button" onClick={() => routeOne(r.id)}>路由</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
