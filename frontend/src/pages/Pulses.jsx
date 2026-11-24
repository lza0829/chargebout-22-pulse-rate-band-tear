import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Pulses() {
  const [list, setList] = useState([])
  const [id, setId] = useState('')
  const [kwh, setKwh] = useState('1.5')
  const [kw, setKw] = useState('7')
  const [msg, setMsg] = useState('')
  const [ok, setOk] = useState(false)
  const load = () =>
    api.会话('充电中').then((rows) => {
      setList(rows)
      if (rows[0] && !id) setId(String(rows[0].id))
    })
  useEffect(() => {
    load()
  }, [])
  const submit = async () => {
    setMsg('')
    setOk(false)
    try {
      const r = await api.脉冲(Number(id), { 电量: Number(kwh), 功率: Number(kw) })
      setMsg(
        `本笔 ${r.本笔档}档 ×${r.本笔单价来源} ${r.本笔单价} = ${r.本笔金额}；` +
          `累计电量 ${r.累计电量}，预估金额 ${r.预估金额}；对账${r.对账一致 ? '一致' : '撕裂'}，脉冲灯 ${r.脉冲灯}`,
      )
      setOk(r.对账一致)
      load()
    } catch (e) {
      setOk(false)
      setMsg(`${e.message}（可到会话册对账后再记）`)
    }
  }
  return (
    <div className="page">
      <h2>记脉冲</h2>
      <p className="lead">
        给充电中会话追加电量脉冲。每笔脉冲按<b>脉冲时刻所属档 × 会话锁档价</b>落价，
        预估金额由全部脉冲累进；写入即对账，档价对不上整笔回滚。
      </p>
      <div className="panel">
        <div className="row">
          <label>会话</label>
          <select value={id} onChange={(e) => setId(e.target.value)}>
            {list.map((s) => (
              <option key={s.id} value={s.id}>
                {s.slip_no} · {s.pile_name} · 已充 {s.kwh} · {s.金额一致 ? '对账一致' : '对账拧花'}
              </option>
            ))}
          </select>
          <label>电量</label>
          <input value={kwh} onChange={(e) => setKwh(e.target.value)} style={{ width: 90 }} />
          <label>功率</label>
          <input value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 90 }} />
          <button type="button" className="primary" onClick={submit}>
            写入脉冲
          </button>
        </div>
        <p className={`msg ${ok ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
