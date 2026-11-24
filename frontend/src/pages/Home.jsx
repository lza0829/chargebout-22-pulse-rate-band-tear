import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    api
      .总览()
      .then(setO)
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
  }, [])
  if (!o) return <div className="page"><p className="msg">{err || '加载中'}</p></div>
  const items = [
    ['桩位空闲', o.桩位空闲],
    ['桩位占用', o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
    ['充电中', o.充电中],
    ['结算中', o.结算中],
    ['已关账', o.已关账],
    ['已超时', o.已超时],
    ['已中断', o.已中断],
    ['在充电量', o.在充电量],
    ['在充预估金额', o.在充预估金额],
    ['待核对账金额', o.待核对账金额],
    ['本日关账金额', o.本日关账金额],
    ['对账异常会话', o.对账异常会话数],
    ['占用态异常', o.占用态对账异常],
  ]
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'} · 档价口径 {o.档价口径}
      </p>
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>
      {(o.对账异常会话 || []).length > 0 && (
        <div className="panel">
          <h3>对账异常会话（不计入已对齐金额）</h3>
          <table>
            <thead>
              <tr>
                <th>会话号</th>
                <th>桩位</th>
                <th>状态</th>
                <th>问题</th>
              </tr>
            </thead>
            <tbody>
              {o.对账异常会话.map((x) => (
                <tr key={x.会话编号}>
                  <td>{x.会话号}</td>
                  <td>{x.桩位}</td>
                  <td>{x.状态}</td>
                  <td>{x.问题.join('；')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(o.关账金额对账异常 || []).length > 0 && (
        <div className="panel">
          <h3>关账金额对账异常</h3>
          <table>
            <thead>
              <tr>
                <th>会话号</th>
                <th>缓存金额</th>
                <th>重放金额</th>
              </tr>
            </thead>
            <tbody>
              {o.关账金额对账异常.map((x) => (
                <tr key={x.会话编号}>
                  <td>{x.会话号}</td>
                  <td>{x.缓存金额}</td>
                  <td>{x.重放金额}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
