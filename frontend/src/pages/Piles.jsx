import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Piles() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () => api.桩位().then(setList)
  useEffect(() => {
    load()
  }, [])
  const setStatus = async (id, 状态) => {
    try {
      await api.改桩态(id, 状态)
      setMsg(`桩位 ${id} → ${状态}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className="page">
      <h2>桩位</h2>
      <p className="lead">场站下桩位名册与空闲占用故障维护态；在充金额按会话锁档重放，不假亮。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>站点</th>
              <th>桩名</th>
              <th>额定功率</th>
              <th>费率</th>
              <th>状态</th>
              <th>在充会话</th>
              <th>在充电量</th>
              <th>在充金额（重放）</th>
              <th>对账</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((p) => (
              <tr key={p.id}>
                <td>{p.site_name}</td>
                <td>{p.name}</td>
                <td>{p.rated_kw}</td>
                <td>{p.rate_name}</td>
                <td>
                  <span className={`tag ${p.status}`}>{p.status}</span>
                </td>
                <td>{p.open_slip_no || '—'}</td>
                <td>{p.open_session_id ? p.在充电量 : '—'}</td>
                <td>{p.open_session_id ? p.在充金额 : '—'}</td>
                <td>
                  {p.open_session_id ? (
                    <span className={`tag ${p.对账一致 ? '已关账' : '已中断'}`}>
                      {p.对账一致 ? '一致' : '撕裂'}
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="row" style={{ border: 0 }}>
                  {['空闲', '维护', '故障'].map((s) => (
                    <button key={s} type="button" onClick={() => setStatus(p.id, s)}>
                      {s}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
