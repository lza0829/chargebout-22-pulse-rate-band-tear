import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settle() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([api.会话('充电中'), api.会话('已超时'), api.会话('结算中')]).then(
      ([a, b, c]) => setList([...a, ...b, ...c]),
    )
  useEffect(() => {
    load()
  }, [])
  const go = async (id) => {
    setMsg('')
    try {
      const r = await api.结算(id)
      setMsg(`已关账：电量 ${r.电量}，金额 ${r.金额}（${r.档价口径}，对账${r.对账一致 ? '一致' : '撕裂'}）`)
      load()
    } catch (e) {
      setMsg(`${e.message}（请先到会话册对该会话对账）`)
    }
  }
  return (
    <div className="page">
      <h2>结算关账</h2>
      <p className="lead">
        按会话锁档逐笔重放脉冲金额关账，释放桩位与枪线。档价证据对不上的会话会被拒关，需先对账。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>缓存金额</th>
              <th>重放金额</th>
              <th>对账</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.replay_amount}</td>
                <td>
                  <span className={`tag ${s.金额一致 ? '已关账' : '已中断'}`}>
                    {s.金额一致 ? '一致' : '拧花'}
                  </span>
                </td>
                <td>
                  <button type="button" className="primary" onClick={() => go(s.id)}>
                    结算关账
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`msg ${msg.startsWith('已关账') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
