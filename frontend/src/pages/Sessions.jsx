import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  useEffect(() => {
    api.会话().then(setList)
  }, [])
  const canon = detail?.开充档价快照
  const replay = detail?.重放
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>会话册</h2>
      <p className="lead">
        最近会话列表；点开可看脉冲、每笔档价与预估重放。档价口径：<b>逐笔脉冲落价</b>。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额（预估/重放）</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => api.会话详情(s.id).then(setDetail)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <p>
            档价口径 {detail.档价口径} · 电量 {detail.kwh} · 预估金额 {detail.预估金额} · 重放金额{' '}
            {detail.重放金额} ·{' '}
            <span className={`tag ${detail.预估与重放一致 ? '' : '已中断'}`}>
              {detail.预估与重放一致 ? '预估与重放一致' : '预估与重放拧花'}
            </span>
          </p>
          <p>
            开充档价快照：{canon?.档 || '—'}档 {canon?.单价 ?? '—'}（{canon?.说明}）
          </p>
          <p>
            费率当前档价：峰 {detail.费率当前档价?.峰} · 平 {detail.费率当前档价?.平} · 谷{' '}
            {detail.费率当前档价?.谷}
          </p>
          <h3>脉冲（逐笔落价）</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
                <th>档</th>
                <th>单价</th>
                <th>金额</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                  <td>{p.band || '—'}</td>
                  <td>{p.unit_price ?? '—'}</td>
                  <td>{p.amount ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>预估重放分档</h3>
          <table>
            <thead>
              <tr>
                <th>档</th>
                <th>笔数</th>
                <th>电量</th>
                <th>金额</th>
              </tr>
            </thead>
            <tbody>
              {(replay?.分档 || []).map((b) => (
                <tr key={b.band}>
                  <td>{b.band || '—'}</td>
                  <td>{b.n}</td>
                  <td>{b.kwh}</td>
                  <td>{b.amount}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
