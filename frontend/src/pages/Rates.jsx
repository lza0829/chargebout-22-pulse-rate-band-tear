import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Rates() {
  const [list, setList] = useState([])
  const [changes, setChanges] = useState([])
  const [audit, setAudit] = useState(null)
  const [edit, setEdit] = useState(null)
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([api.费率(), api.改档记录(), api.脉冲审计(100)]).then(([r, c, a]) => {
      setList(r)
      setChanges(c)
      setAudit(a)
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
          `${r.说明}（当前在途会话 ${r.在途会话数} 笔，均沿用各自锁档）`,
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
        峰平谷电价档。档价口径：<b>会话锁档计价</b>——开充时冻结整张峰平谷表为该会话锁档，
        每笔脉冲按脉冲时刻档 × 会话锁档价落价；改档只影响此后新开会话，不回写任何旧会话。
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
              <th>在途会话</th>
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
                <td>{r.在途会话数}</td>
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

      <div className="panel">
        <h3>脉冲审计（逐笔对会话锁档）· 异常 {audit?.异常数 ?? 0}</h3>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>会话</th>
              <th>电量</th>
              <th>档</th>
              <th>单价</th>
              <th>预期锁档价</th>
              <th>金额</th>
              <th>档价</th>
              <th>金额核</th>
            </tr>
          </thead>
          <tbody>
            {(audit?.脉冲 || []).map((p) => (
              <tr key={p.id}>
                <td>{p.created_at}</td>
                <td>{p.slip_no}</td>
                <td>{p.delta_kwh}</td>
                <td>{p.band}</td>
                <td>{p.unit_price}</td>
                <td>{p.预期锁档单价}</td>
                <td>{p.amount}</td>
                <td>
                  <span className={`tag ${p.档价一致 ? '已关账' : '已中断'}`}>
                    {p.档价一致 ? '一致' : '拧花'}
                  </span>
                </td>
                <td>
                  <span className={`tag ${p.金额一致 ? '已关账' : '已中断'}`}>
                    {p.金额一致 ? '一致' : '拧花'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
