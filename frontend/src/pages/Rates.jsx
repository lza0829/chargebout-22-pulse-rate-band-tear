import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Rates() {
  const [list, setList] = useState([])
  const [changes, setChanges] = useState([])
  const [edit, setEdit] = useState(null)
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([api.费率(), api.改档记录()]).then(([r, c]) => {
      setList(r)
      setChanges(c)
    })
  useEffect(() => {
    load()
  }, [])
  const startEdit = (r) =>
    setEdit({ id: r.id, 名称: r.name, 峰价: String(r.peak_price), 平价: String(r.flat_price), 谷价: String(r.valley_price) })
  const submit = async () => {
    setMsg('')
    try {
      const r = await api.改费率(edit.id, {
        峰价: Number(edit.峰价),
        平价: Number(edit.平价),
        谷价: Number(edit.谷价),
        备注: '费率页改档',
      })
      setMsg(
        `已改 ${r.名称}：峰 ${r.旧.峰}→${r.新.峰}，平 ${r.旧.平}→${r.新.平}，谷 ${r.旧.谷}→${r.新.谷}。` +
          '仅影响此后写入脉冲，在途会话已落价脉冲与开充快照不变。',
      )
      setEdit(null)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>费率</h2>
      <p className="lead">
        峰平谷电价档。档价口径：<b>逐笔脉冲落价</b>——每笔脉冲按写入时刻档价落价，
        开充快照只作开场记录，改档不回写任何会话。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>峰价</th>
              <th>平价</th>
              <th>谷价</th>
              <th>当前档</th>
              <th>当前档价</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{r.peak_price}</td>
                <td>{r.flat_price}</td>
                <td>{r.valley_price}</td>
                <td>
                  <span className="tag">{r.当前档}</span>
                </td>
                <td>{r.当前档价}</td>
                <td>
                  <button type="button" onClick={() => startEdit(r)}>
                    改档
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {edit && (
          <div className="row" style={{ marginTop: '0.8rem' }}>
            <b>{edit.名称}</b>
            <label>峰价</label>
            <input value={edit.峰价} onChange={(e) => setEdit({ ...edit, 峰价: e.target.value })} style={{ width: 90 }} />
            <label>平价</label>
            <input value={edit.平价} onChange={(e) => setEdit({ ...edit, 平价: e.target.value })} style={{ width: 90 }} />
            <label>谷价</label>
            <input value={edit.谷价} onChange={(e) => setEdit({ ...edit, 谷价: e.target.value })} style={{ width: 90 }} />
            <button type="button" className="primary" onClick={submit}>
              提交改档
            </button>
            <button type="button" onClick={() => setEdit(null)}>
              取消
            </button>
          </div>
        )}
        <p className={`msg ${msg.startsWith('已改') ? 'ok' : ''}`}>{msg}</p>
      </div>

      <div className="panel">
        <h3>改档审计流水</h3>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>费率</th>
              <th>峰</th>
              <th>平</th>
              <th>谷</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            {changes.map((c) => (
              <tr key={c.id}>
                <td>{c.changed_at}</td>
                <td>{c.plan_name}</td>
                <td>
                  {c.old_peak} → {c.new_peak}
                </td>
                <td>
                  {c.old_flat} → {c.new_flat}
                </td>
                <td>
                  {c.old_valley} → {c.new_valley}
                </td>
                <td>{c.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
