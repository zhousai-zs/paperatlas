import argparse
import json
import sys
from pathlib import Path

from .core import Atlas
from .embedding import prepare
from .errors import AtlasError


def main(argv=None):
    parser = argparse.ArgumentParser(prog='paperatlas', description='本地论文检索与来源定位')
    parser.add_argument('--data-dir', default=str(Path.cwd() / '.paperatlas'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    models = sub.add_parser('models').add_subparsers(dest='action', required=True)
    prepare_parser = models.add_parser('prepare')
    prepare_parser.add_argument('--revision', default='main')
    index = sub.add_parser('index')
    index.add_argument('path')
    index.add_argument('--mode', choices=['hybrid', 'lexical'], default='hybrid')
    listing = sub.add_parser('list')
    listing.add_argument('--limit', type=int, default=50)
    listing.add_argument('--offset', type=int, default=0)
    search = sub.add_parser('search')
    search.add_argument('query')
    search.add_argument('--top-k', type=int, default=5)
    search.add_argument('--mode', choices=['hybrid', 'lexical', 'semantic'], default='hybrid')
    search.add_argument('--doc-id')
    read = sub.add_parser('read')
    read.add_argument('doc_id')
    read.add_argument('--page', type=int, required=True)
    for command in [index, listing, search, read]:
        command.add_argument('--json', action='store_true')
    sub.add_parser('mcp')
    args = parser.parse_args(argv)
    atlas = None
    try:
        if args.command == 'models':
            output = prepare(args.data_dir, args.revision)
        elif args.command == 'mcp':
            from .mcp_server import serve
            serve(args.data_dir)
            return 0
        else:
            atlas = Atlas(args.data_dir, create=args.command == 'init')
            if args.command == 'init':
                output = dict(database=str(atlas.db.directory / 'atlas.db'), status='ready')
            elif args.command == 'index':
                output = atlas.index(args.path, args.mode)
            elif args.command == 'list':
                output = atlas.list_papers(args.limit, args.offset)
            elif args.command == 'search':
                output = atlas.search_papers(args.query, args.top_k, args.doc_id, args.mode)
            else:
                output = atlas.read_page(args.doc_id, args.page)
        if getattr(args, 'json', False) or args.command in {'init', 'models', 'index', 'list'}:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        elif args.command == 'read':
            print(f"{output['title']} — PDF 第 {output['page']} 页\n" + '\n'.join(output['warnings']))
            print(output['markdown'])
            print(json.dumps(output['source'], ensure_ascii=False, indent=2))
        else:
            print(output['notice'])
            for item in output['results']:
                print(f"\n{item['title']} — PDF 第 {item['page']} 页 [{item['status']}]\n{item['doc_id']}\n{item['excerpt']}\n{item['source']['page_image']}")
                print('\n'.join(item['warnings']))
        return 1 if args.command == 'index' and any(r['result'] == 'failed' for r in output['results']) else 0
    except (AtlasError, OSError, ValueError) as exc:
        print(f'PaperAtlas: {exc}', file=sys.stderr)
        return 2
    finally:
        if atlas:
            atlas.close()


if __name__ == '__main__':
    raise SystemExit(main())
