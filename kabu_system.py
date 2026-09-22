"""Research and opt-in continuous paper operations CLI; no live order support."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from system_data import provider, daily_groups, encode, save_snapshot, digest
from jquants_history import HistoryError
from system_features import dataset
from system_backtest import run, Account
from system_strategy import MovingAverage
from system_risk import RiskLimits
from system_news import NewsStore
from system_integration import IntegratedStrategy

ROOT = Path(__file__).resolve().parent


def output_folder():
    return ROOT/'data'/('system_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))


def load_bars(args):
    symbols = args.symbols or json.loads((ROOT/'symbols_10.json').read_text(encoding='utf-8-sig'))['symbols']
    bars = provider(args.prices_db).bars(symbols)
    daily_groups(bars)
    return bars


def news_store(args):
    if bool(args.coverage_start) != bool(args.coverage_end):
        raise ValueError('Both coverage boundaries required')
    coverage = [args.coverage_start,args.coverage_end] if args.coverage_start else None
    return NewsStore(args.news_db,coverage)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['normalize','train','evaluate','compare','predict','paper-step','paper-report','run','run-status'])
    p.add_argument('--config',type=Path,default=ROOT/'config/paper.json')
    p.add_argument('--once',action='store_true',help='自動運転を1巡だけ実行')
    p.add_argument('--actions-db',type=Path,help='確認済み企業行動の台帳')
    p.add_argument('--prices-db',type=Path,default=ROOT/'data/yahoo.sqlite3')
    p.add_argument('--symbols',nargs='+')
    p.add_argument('--model',type=Path)
    p.add_argument('--combined-model',type=Path)
    p.add_argument('--kind',choices=['chart','combined'],default='chart')
    p.add_argument('--news-db',type=Path)
    p.add_argument('--coverage-start',help='Explicitly verified collector coverage, ISO timezone timestamp')
    p.add_argument('--coverage-end')
    p.add_argument('--horizon',type=int,default=5)
    p.add_argument('--threshold',type=float,default=0)
    p.add_argument('--output',type=Path)
    p.add_argument('--cash',type=float,default=10_000_000)
    p.add_argument('--quantity',type=int,default=100)
    p.add_argument('--fee',type=float,default=0)
    p.add_argument('--slippage-bps',type=float,default=5)
    p.add_argument('--spread-bps',type=float,default=0)
    p.add_argument('--per-symbol-limit',type=float,default=1_000_000)
    p.add_argument('--total-limit',type=float,default=5_000_000)
    p.add_argument('--max-holdings',type=int,default=10)
    p.add_argument('--daily-loss-limit',type=float,default=100_000)
    p.add_argument('--daily-order-limit',type=int,default=20)
    p.add_argument('--mode',choices=['research','recorded'],default='research')
    p.add_argument('--ticks-db',type=Path,default=ROOT/'data/production.sqlite3')
    p.add_argument('--ledger',type=Path,default=ROOT/'data/system_paper.sqlite3')
    p.add_argument('--stop-file',type=Path,default=ROOT/'data/STOP_NEW_TRADES')
    args = p.parse_args(argv)
    try:
        if args.command=='run':
            from system_runtime import run_config
            run_config(args.config,args.once)
            return 0
        if args.command=='run-status':
            from system_runtime import read_config,connect
            from contextlib import closing
            c=read_config(args.config)
            with closing(connect(c['paths']['runtime'])) as db:
                print(encode({'jobs':db.execute('SELECT * FROM jobs').fetchall(),
                              'logs':db.execute('SELECT at,job,status,detail FROM logs ORDER BY id DESC LIMIT 20').fetchall()}))
            return 0
        if args.command=='paper-report':
            from system_paper import report
            events=report(args.ledger)
            print(encode(events[-5:]))
            return 0
        bars = load_bars(args)
        news = news_store(args)
        if args.command=='normalize':
            target=args.output or ROOT/'data/normalized.sqlite3'
            if target.resolve()==args.prices_db.resolve():
                raise ValueError('Output must differ from source database')
            save_snapshot(target,bars)
            print(f'{len(bars)} bars / {target.resolve()} / original DB unchanged')
            return 0
        if args.command=='train':
            from system_model import train
            rows=dataset(bars,args.horizon)
            if args.kind=='combined':
                rows=news.augment(rows)
            target=args.output or output_folder()
            result=train(rows,target,bars[0].source,args.kind)
            if result['status']=='OK':
                save_snapshot(target/'normalized.sqlite3',bars)
            print(encode(result))
            return 0
        limits=RiskLimits(args.per_symbol_limit,args.total_limit,args.max_holdings,
                          args.daily_loss_limit,args.daily_order_limit)
        limits.validate()
        chart=MovingAverage()
        if args.model:
            from system_model import ReturnModel
            chart=ReturnModel(args.model,args.threshold,news)
        if args.command in ('evaluate','compare','predict') and not args.model:
            raise ValueError('--model required; train a chart model first')
        if args.command=='predict':
            predictions=[chart.predict(g).record() for g in daily_groups(bars).values()]
            target=args.output or output_folder()
            target.mkdir(parents=True,exist_ok=False)
            (target/'predictions.json').write_text(encode(predictions),encoding='utf-8')
            print(encode(predictions))
            print(target.resolve())
            return 0
        if args.command=='paper-step':
            from system_paper import step
            # Paper quantity is currently fixed at one 100-share lot.
            if args.quantity!=100:
                raise ValueError('Forward paper currently supports --quantity 100 only')
            strategy=IntegratedStrategy(chart,news) if args.news_db else chart
            account=Account(args.cash,limits,args.fee,args.slippage_bps,args.spread_bps)
            from corporate_actions import ConfirmedActions
            print(encode(step(args.ledger,bars,args.ticks_db,strategy,account,stop_new=args.stop_file.exists(),
                              actions=ConfirmedActions(args.actions_db) if args.actions_db else None)))
            return 0
        start=chart.metadata['boundaries']['test_start']
        end=chart.metadata['ranges']['test'][1]
        evaluation_rows=dataset(bars,chart.metadata['horizon'])
        if chart.metadata['kind']=='combined':
            evaluation_rows=news.augment(evaluation_rows)
        if digest(evaluation_rows)!=chart.metadata['dataset_hash']:
            raise ValueError('Evaluation data changed since training. Preserve the input snapshot or train a new model; saved metrics cannot be reused')
        if args.command=='evaluate':
            print('Saved holdout prediction metrics:',encode(chart.metadata['evaluations']['test']))
            strategies={'A-chart':chart}
        else:
            combined=IntegratedStrategy(chart,news)
            if args.combined_model:
                from system_model import ReturnModel
                combined=ReturnModel(args.combined_model,args.threshold,news)
                if combined.metadata['kind']!='combined' or combined.metadata['boundaries']!=chart.metadata['boundaries']:
                    raise ValueError('Combined model must use the same evaluation boundaries')
                if digest(news.augment(dataset(bars,combined.metadata['horizon'])))!=combined.metadata['dataset_hash']:
                    raise ValueError('Combined model training data/news differ from evaluation inputs')
            strategies={'MA-baseline':MovingAverage(),'A-chart':chart,
                        'B-news-events':IntegratedStrategy(chart,news,news_only=True),'C-combined':combined}
        reports={name:run(bars,strategy,args.cash,args.quantity,limits,args.fee,args.slippage_bps,args.spread_bps,
                          args.mode,start,end) for name,strategy in strategies.items()}
        target=args.output or output_folder()
        target.mkdir(parents=True,exist_ok=False)
        save_snapshot(target/'normalized.sqlite3',bars)
        for name,report in reports.items():
            if name=='B-news-events':
                report['evaluation_scope']='News-event opportunities only; cash curve shares the A/C calendar, not the same prediction sample'
                report['event_opportunities']=sum(bool(d['prediction']['refs']) for d in report['decisions'])
            (target/(name+'.json')).write_text(encode(report),encoding='utf-8')
            print(name,encode({k:report[k] for k in ('net_pnl','max_drawdown_pct','trade_count','fees','execution_cost')}))
        (target/'news_snapshot.json').write_text(encode(news.items),encoding='utf-8')
        from system_plot import render
        render(reports,bars,target)
        print('Result:',target.resolve())
        print('Offline simulation only. B uses news events; A retains all eligible chart sessions.')
        return 0
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,ImportError,HistoryError) as exc:
        # Only locally generated validation errors are shown; never API response bodies.
        print('Stopped:',type(exc).__name__,str(exc))
        if args.command=='paper-step':
            from system_paper import record_error
            record_error(args.ledger,type(exc).__name__+': '+str(exc))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
