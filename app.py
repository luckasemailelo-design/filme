import os
import json
import logging
import re
import random
import string
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, abort
from database import init_db, db
from models import Usuario, Canal, Favorito, Progresso
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy import func, desc

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Troque em produção
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

init_db(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Configuração da TMDB ----------
TMDB_API_KEY = os.environ.get('TMDB_API_KEY', 'dcc7930e96fc6ef24e8711d614b9071e')
TMDB_BASE_URL = 'https://api.themoviedb.org/3'
TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/w500'

def buscar_filme_por_titulo(titulo):
    url = f"{TMDB_BASE_URL}/search/movie"
    params = {'api_key': TMDB_API_KEY, 'query': titulo, 'language': 'pt-BR'}
    try:
        resp = requests.get(url, params=params)
        dados = resp.json()
        if dados.get('results'):
            filme = dados['results'][0]
            return {
                'sinopse': filme.get('overview', 'Sinopse não disponível'),
                'poster': f"{TMDB_IMAGE_BASE}{filme['poster_path']}" if filme.get('poster_path') else None
            }
    except Exception as e:
        logger.error(f"Erro na busca TMDB: {e}")
    return {'sinopse': 'Sinopse não encontrada', 'poster': None}

def buscar_serie_por_titulo(titulo):
    url = f"{TMDB_BASE_URL}/search/tv"
    params = {'api_key': TMDB_API_KEY, 'query': titulo, 'language': 'pt-BR'}
    try:
        resp = requests.get(url, params=params)
        dados = resp.json()
        if dados.get('results'):
            serie = dados['results'][0]
            return {
                'id': serie['id'],
                'sinopse': serie.get('overview', 'Sinopse não disponível'),
                'poster': f"{TMDB_IMAGE_BASE}{serie['poster_path']}" if serie.get('poster_path') else None
            }
    except Exception as e:
        logger.error(f"Erro na busca TMDB: {e}")
    return {'id': None, 'sinopse': 'Sinopse não encontrada', 'poster': None}

def buscar_episodio(series_id, temporada, episodio):
    url = f"{TMDB_BASE_URL}/tv/{series_id}/season/{temporada}/episode/{episodio}"
    params = {'api_key': TMDB_API_KEY, 'language': 'pt-BR'}
    try:
        resp = requests.get(url, params=params)
        dados = resp.json()
        return dados.get('overview', 'Sinopse do episódio não disponível')
    except Exception as e:
        logger.error(f"Erro na busca do episódio: {e}")
        return 'Sinopse não encontrada'

# ---------- Funções auxiliares para carregar JSON ----------
def carregar_json_no_banco():
    if Canal.query.first() is not None:
        logger.info("Banco já contém dados. Nenhuma carga realizada.")
        return

    json_dir = 'm3u'
    json_path = os.path.join(json_dir, 'lista.json')
    if not os.path.exists(json_path):
        logger.warning(f"Arquivo {json_path} não encontrado.")
        return

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            dados = json.load(f)
    except Exception as e:
        logger.error(f"Erro ao ler JSON: {e}")
        return

    if not isinstance(dados, list):
        logger.error("Formato JSON inválido: esperava uma lista.")
        return

    for item in dados:
        nome = item.get('nome', '')
        logo = item.get('logo', '')
        tipo_original = item.get('tipo', '')
        categoria = item.get('categoria', '')
        temporada = item.get('temporada')
        episodio = item.get('episodio')
        url = item.get('url', '')
        ano_lancamento = item.get('ano_lancamento', '')

        if tipo_original.lower() == 'radio':
            tipo = 'radio'
        elif tipo_original.lower() == 'series':
            tipo = 'serie'
        elif tipo_original.lower() == 'filmes':
            tipo = 'filme'
        else:
            tipo = 'tv'

        canal = Canal(
            nome=nome,
            url=url,
            logo=logo,
            grupo='',
            tvg_id='',
            tipo=tipo,
            categoria=categoria,
            temporada=temporada if temporada is not None else None,
            episodio=episodio if episodio is not None else None,
            ano_lancamento=ano_lancamento,
            tmdb_id=None,
            sinopse_geral=None,
            sinopse_episodio=None
        )
        if tipo == 'serie':
            match = re.search(r'S(\d+)E(\d+)', nome, re.IGNORECASE)
            if match:
                canal.serie_nome = re.sub(r'S\d+E\d+', '', nome, flags=re.IGNORECASE).strip()
            else:
                canal.serie_nome = nome
        db.session.add(canal)

    db.session.commit()
    logger.info(f"{len(dados)} itens carregados do JSON.")

# ---------- Decorador para verificar admin ----------
def admin_required(f):
    def decorated_function(*args, **kwargs):
        if 'usuario_id' not in session:
            return redirect(url_for('login'))
        usuario = Usuario.query.get(session['usuario_id'])
        if not usuario or not usuario.is_admin:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# ---------- Context processor ----------
@app.context_processor
def inject_user():
    if 'usuario_id' in session:
        usuario = Usuario.query.get(session['usuario_id'])
        return dict(usuario_atual=usuario)
    return dict(usuario_atual=None)

# ---------- Função para filtrar adultos ----------
def filtrar_adultos(query):
    """Exclui itens com categoria 'Adultos'."""
    return query.filter((Canal.categoria != 'Adultos') | (Canal.categoria.is_(None)))

# ---------- Rotas principais ----------
@app.route('/')
def index():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    usuario = Usuario.query.get(session['usuario_id'])
    usuario.ultimo_acesso = datetime.utcnow()
    db.session.commit()
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        senha = request.form['senha']
        usuario = Usuario.query.filter_by(email=email).first()
        if usuario and check_password_hash(usuario.senha, senha):
            if not usuario.ativo:
                return render_template('login.html', erro='Conta desativada. Contate o administrador.')
            if not usuario.is_admin and usuario.expira_em and usuario.expira_em < datetime.utcnow():
                return render_template('login.html', erro='Conta expirada. Contate o administrador.')
            session['usuario_id'] = usuario.id
            usuario.ultimo_acesso = datetime.utcnow()
            db.session.commit()
            return redirect(url_for('index'))
        return render_template('login.html', erro='Email ou senha inválidos')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
@admin_required
def register():
    if request.method == 'POST':
        nome = request.form['nome']
        email = request.form['email']
        senha = request.form['senha']
        dias = request.form.get('dias', type=int)
        is_admin = request.form.get('is_admin') == 'on'

        if Usuario.query.filter_by(email=email).first():
            # Redireciona de volta para admin com mensagem de erro
            return redirect(url_for('admin', erro='Email já cadastrado'))

        hash_senha = generate_password_hash(senha)
        expira_em = None
        if dias and dias > 0 and not is_admin:
            expira_em = datetime.utcnow() + timedelta(days=dias)

        usuario = Usuario(
            nome=nome,
            email=email,
            senha=hash_senha,
            is_admin=is_admin,
            expira_em=expira_em
        )
        db.session.add(usuario)
        db.session.commit()
        return redirect(url_for('admin'))
    # Se for GET, redireciona para admin (não deveria ser acessado diretamente)
    return redirect(url_for('admin'))

@app.route('/logout')
def logout():
    session.pop('usuario_id', None)
    return redirect(url_for('login'))

@app.route('/series')
def series():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    return render_template('series.html')

@app.route('/filmes')
def filmes():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    return render_template('filmes.html')

# ==================== ROTAS DE DETALHE COM CACHE E FILTRO ADULTOS ====================
@app.route('/serie/<nome>')
def serie_detalhe(nome):
    if 'usuario_id' not in session:
        return redirect(url_for('login'))

    episodios = Canal.query.filter_by(tipo='serie', serie_nome=nome).order_by(
        Canal.temporada, Canal.episodio).all()
    if not episodios:
        return redirect(url_for('series'))

    # Verifica se algum episódio é adulto (se a série for adulta, todos os episódios provavelmente são)
    if any(ep.categoria == 'Adultos' for ep in episodios):
        abort(404)  # ou redireciona para página de erro

    # Usa o primeiro episódio como referência para dados da série
    serie_principal = episodios[0]
    sinopse_geral = serie_principal.sinopse_geral
    poster_serie = serie_principal.logo
    series_id = serie_principal.tmdb_id

    # Se não tem sinopse geral ou ID, busca na TMDB e salva
    if not sinopse_geral or not series_id:
        dados_serie = buscar_serie_por_titulo(nome)
        if dados_serie.get('id'):
            series_id = dados_serie['id']
            sinopse_geral = dados_serie.get('sinopse', 'Sinopse não disponível')
            poster_serie = dados_serie.get('poster') or serie_principal.logo
            # Atualiza todos os episódios da série
            for ep in episodios:
                ep.tmdb_id = series_id
                ep.sinopse_geral = sinopse_geral
            db.session.commit()
        else:
            sinopse_geral = 'Sinopse não encontrada'

    # Para cada episódio, verifica sinopse individual
    for ep in episodios:
        if not ep.sinopse_episodio and series_id and ep.temporada and ep.episodio:
            sinopse_ep = buscar_episodio(series_id, ep.temporada, ep.episodio)
            ep.sinopse_episodio = sinopse_ep
            db.session.commit()
        elif not ep.sinopse_episodio:
            ep.sinopse_episodio = 'Sinopse não disponível'

    # Organiza por temporadas
    temporadas = {}
    for ep in episodios:
        temp = ep.temporada
        if temp not in temporadas:
            temporadas[temp] = []
        temporadas[temp].append(ep)

    return render_template('serie-detalhe.html',
                           serie_nome=nome,
                           temporadas=temporadas,
                           sinopse_geral=sinopse_geral,
                           poster_serie=poster_serie)

@app.route('/filme/<int:id>')
def filme_detalhe(id):
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    filme = Canal.query.get_or_404(id)
    if filme.categoria == 'Adultos':
        abort(404)
    if not filme.sinopse_geral:
        dados_tmdb = buscar_filme_por_titulo(filme.nome)
        sinopse = dados_tmdb.get('sinopse', 'Sinopse não encontrada')
        poster_tmdb = dados_tmdb.get('poster')
        filme.sinopse_geral = sinopse
        if poster_tmdb:
            filme.logo = poster_tmdb
        db.session.commit()
    else:
        sinopse = filme.sinopse_geral
        poster_tmdb = filme.logo
    return render_template('filme-detalhe.html', filme=filme, sinopse=sinopse, poster_tmdb=poster_tmdb)

@app.route('/play/<int:id>')
def play(id):
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    canal = Canal.query.get_or_404(id)
    if canal.categoria == 'Adultos':
        abort(404)
    proximo = None
    if canal.tipo == 'serie' and canal.serie_nome and canal.temporada is not None and canal.episodio is not None:
        # Ao buscar próximo episódio, também filtrar adultos (embora já estejam filtrados)
        proximo = Canal.query.filter(
            Canal.tipo == 'serie',
            Canal.serie_nome == canal.serie_nome,
            ((Canal.temporada == canal.temporada) & (Canal.episodio > canal.episodio)) |
            ((Canal.temporada == canal.temporada + 1) & (Canal.episodio == 1)),
            Canal.categoria != 'Adultos'  # segurança extra
        ).order_by(Canal.temporada, Canal.episodio).first()
    return render_template('player.html', canal=canal, proximo_episodio=proximo)

@app.route('/favoritos')
def favoritos():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    usuario_id = session['usuario_id']
    favs = Favorito.query.filter_by(usuario_id=usuario_id).all()
    # Filtrar favoritos adultos (opcional, mas por segurança)
    favs = [f for f in favs if f.canal and f.canal.categoria != 'Adultos']
    return render_template('favoritos.html', favoritos=favs)

@app.route('/perfil')
def perfil():
    if 'usuario_id' not in session:
        return redirect(url_for('login'))
    usuario = Usuario.query.get(session['usuario_id'])
    return render_template('perfil.html', usuario=usuario)

@app.route('/busca')
def busca():
    termo = request.args.get('q', '')
    return render_template('resultados.html', termo=termo)

# ---------- Área Admin ----------
@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html')

@app.route('/api/admin/estatisticas')
@admin_required
def api_admin_estatisticas():
    cinco_min_atras = datetime.utcnow() - timedelta(minutes=5)
    online = Usuario.query.filter(Usuario.ultimo_acesso >= cinco_min_atras).count()
    total_usuarios = Usuario.query.count()
    total_admins = Usuario.query.filter_by(is_admin=True).count()
    total_segundos = db.session.query(func.sum(Progresso.tempo)).scalar() or 0
    total_horas = total_segundos // 3600
    return jsonify({
        'online': online,
        'total_usuarios': total_usuarios,
        'total_horas': total_horas,
        'total_admins': total_admins
    })

@app.route('/api/admin/usuarios')
@admin_required
def api_admin_usuarios():
    pagina = int(request.args.get('pagina', 1))
    busca = request.args.get('busca', '').strip()
    por_pagina = 20
    query = Usuario.query
    if busca:
        query = query.filter(Usuario.nome.ilike(f'%{busca}%') | Usuario.email.ilike(f'%{busca}%'))
    total = query.count()
    usuarios = query.order_by(Usuario.nome).paginate(page=pagina, per_page=por_pagina, error_out=False)
    return jsonify({
        'itens': [{
            'id': u.id,
            'nome': u.nome,
            'email': u.email,
            'is_admin': u.is_admin,
            'ativo': u.ativo,
            'expira_em': u.expira_em.strftime('%d/%m/%Y') if u.expira_em else None
        } for u in usuarios.items],
        'total': total,
        'pagina': pagina,
        'total_paginas': usuarios.pages
    })

@app.route('/api/admin/usuarios/<int:usuario_id>/banir', methods=['POST'])
@admin_required
def admin_banir_usuario(usuario_id):
    usuario = Usuario.query.get_or_404(usuario_id)
    if usuario.id == session['usuario_id']:
        return jsonify({'erro': 'Você não pode banir a si mesmo'}), 400
    usuario.ativo = not usuario.ativo
    db.session.commit()
    return jsonify({'status': 'ok', 'ativo': usuario.ativo})

@app.route('/api/admin/usuarios/<int:usuario_id>/excluir', methods=['DELETE'])
@admin_required
def admin_excluir_usuario(usuario_id):
    usuario = Usuario.query.get_or_404(usuario_id)
    if usuario.id == session['usuario_id']:
        return jsonify({'erro': 'Você não pode excluir a si mesmo'}), 400
    db.session.delete(usuario)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/admin/usuarios/<int:usuario_id>/resetar-senha', methods=['POST'])
@admin_required
def admin_resetar_senha(usuario_id):
    usuario = Usuario.query.get_or_404(usuario_id)
    nova_senha = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    usuario.senha = generate_password_hash(nova_senha)
    db.session.commit()
    return jsonify({'status': 'ok', 'nova_senha': nova_senha})

@app.route('/api/admin/usuarios/<int:usuario_id>', methods=['GET', 'POST'])
@admin_required
def admin_editar_usuario(usuario_id):
    usuario = Usuario.query.get_or_404(usuario_id)
    if request.method == 'GET':
        return jsonify({
            'id': usuario.id,
            'nome': usuario.nome,
            'email': usuario.email,
            'is_admin': usuario.is_admin,
            'ativo': usuario.ativo,
            'expira_em': usuario.expira_em.strftime('%Y-%m-%d') if usuario.expira_em else None
        })
    else:
        data = request.get_json()
        usuario.nome = data.get('nome', usuario.nome)
        usuario.email = data.get('email', usuario.email)
        usuario.is_admin = data.get('is_admin', usuario.is_admin)
        usuario.ativo = data.get('ativo', usuario.ativo)
        dias = data.get('dias')
        if dias is not None and dias > 0:
            usuario.expira_em = datetime.utcnow() + timedelta(days=dias)
        elif dias == 0:
            usuario.expira_em = None
        db.session.commit()
        return jsonify({'status': 'ok'})

# ---------- API ----------
def get_random_items(tipo, limite=15, ano=None):
    from sqlalchemy.sql.expression import func
    query = Canal.query.filter_by(tipo=tipo)
    if ano:
        query = query.filter_by(ano_lancamento=ano)
    query = filtrar_adultos(query)
    return query.order_by(func.random()).limit(limite).all()

def get_mais_assistidos_global(limite=5):
    progress_counts = db.session.query(
        Progresso.canal_id,
        func.count(Progresso.id).label('total')
    ).group_by(Progresso.canal_id).subquery()

    query = db.session.query(Canal, progress_counts.c.total).join(
        progress_counts, Canal.id == progress_counts.c.canal_id
    )

    # Aplicar filtro de adultos antes de processar
    query = filtrar_adultos(query)

    filmes = query.filter(Canal.tipo == 'filme').order_by(desc(progress_counts.c.total)).all()
    series_raw = query.filter(Canal.tipo == 'serie').all()

    series_map = {}
    for canal, total in series_raw:
        if canal.serie_nome not in series_map:
            series_map[canal.serie_nome] = {'total': 0, 'latest': canal}
        series_map[canal.serie_nome]['total'] += total
        if canal.id > series_map[canal.serie_nome]['latest'].id:
            series_map[canal.serie_nome]['latest'] = canal

    series_list = [(data['latest'], data['total']) for data in series_map.values()]
    series_list.sort(key=lambda x: x[1], reverse=True)

    combined = [(canal, total) for canal, total in filmes] + series_list
    combined.sort(key=lambda x: x[1], reverse=True)

    return [c[0] for c in combined[:limite]]

@app.route('/api/mais-assistidos')
def api_mais_assistidos():
    if 'usuario_id' not in session:
        return jsonify({'erro': 'Não autenticado'}), 401
    itens = get_mais_assistidos_global(5)
    return jsonify([c.serialize() for c in itens])

def get_recentemente_assistidos(usuario_id, limite=15):
    # Subconsulta para séries, já filtrando adultos
    subquery_series = db.session.query(
        Progresso.canal_id,
        Progresso.data_atualizacao,
        func.row_number().over(
            partition_by=Canal.serie_nome,
            order_by=desc(Progresso.data_atualizacao)
        ).label('rn')
    ).join(Canal, Progresso.canal_id == Canal.id).filter(
        Progresso.usuario_id == usuario_id,
        Canal.tipo == 'serie',
        Canal.categoria != 'Adultos'  # filtrar adultos
    ).subquery()

    series_recentes = db.session.query(Progresso).join(
        subquery_series,
        (Progresso.canal_id == subquery_series.c.canal_id) &
        (subquery_series.c.rn == 1)
    ).all()

    # Outros tipos (filmes, tv, radio) filtrando adultos
    outros = Progresso.query.join(Canal).filter(
        Progresso.usuario_id == usuario_id,
        Canal.tipo != 'serie',
        Canal.categoria != 'Adultos'
    ).order_by(desc(Progresso.data_atualizacao)).all()

    todos = series_recentes + outros
    todos.sort(key=lambda p: p.data_atualizacao, reverse=True)
    return [p.canal for p in todos[:limite] if p.canal]

@app.route('/api/inicio')
def api_inicio():
    if 'usuario_id' not in session:
        return jsonify({'erro': 'Não autenticado'}), 401
    usuario_id = session['usuario_id']
    filmes_rec = [c.serialize() for c in get_random_items('filme', 15)]
    series_rec = [c.serialize() for c in get_random_items('serie', 15)]
    recentes = [c.serialize() for c in get_recentemente_assistidos(usuario_id, 15)]
    return jsonify({
        'filmes_recomendados': filmes_rec,
        'series_recomendadas': series_rec,
        'assistido_recentemente': recentes
    })

@app.route('/api/filmes/categoria/<categoria>')
def api_filmes_categoria(categoria):
    query = Canal.query.filter_by(tipo='filme', categoria=categoria)
    query = filtrar_adultos(query)
    filmes = query.limit(15).all()
    return jsonify([f.serialize() for f in filmes])

@app.route('/api/filmes/lancamento')
def api_filmes_lancamento():
    query = Canal.query.filter_by(tipo='filme', ano_lancamento='2026')
    query = filtrar_adultos(query)
    filmes = query.order_by(Canal.id.desc()).limit(15).all()
    return jsonify([f.serialize() for f in filmes])

@app.route('/api/filmes/lista')
def api_filmes_lista():
    pagina = int(request.args.get('pagina', 1))
    ano = request.args.get('ano')
    por_pagina = 20
    query = Canal.query.filter_by(tipo='filme')
    if ano:
        query = query.filter_by(ano_lancamento=ano)
    query = filtrar_adultos(query)
    filmes = query.order_by(Canal.nome).paginate(page=pagina, per_page=por_pagina, error_out=False)
    return jsonify({
        'itens': [f.serialize() for f in filmes.items],
        'total': filmes.total,
        'pagina': pagina,
        'total_paginas': filmes.pages
    })

@app.route('/api/series/categoria/<categoria>')
def api_series_categoria(categoria):
    # Primeiro, obter os ids dos episódios que representam a série, já filtrando adultos
    subquery = db.session.query(Canal.serie_nome, func.min(Canal.id).label('id')).filter(
        Canal.tipo == 'serie',
        Canal.categoria == categoria,
        Canal.categoria != 'Adultos'  # redundante, mas seguro
    ).group_by(Canal.serie_nome).subquery()
    query = db.session.query(Canal).join(subquery, Canal.id == subquery.c.id)
    query = filtrar_adultos(query)
    series = query.limit(15).all()
    return jsonify([s.serialize() for s in series])

@app.route('/api/series/lancamento')
def api_series_lancamento():
    subquery = db.session.query(
        Canal.serie_nome,
        func.min(Canal.id).label('id')
    ).filter(
        Canal.tipo == 'serie',
        Canal.ano_lancamento == '2026',
        Canal.categoria != 'Adultos'
    ).group_by(Canal.serie_nome).subquery()
    query = db.session.query(Canal).join(subquery, Canal.id == subquery.c.id)
    query = filtrar_adultos(query)
    series = query.order_by(Canal.id.desc()).limit(15).all()
    return jsonify([s.serialize() for s in series])

@app.route('/api/series/lista')
def api_series_lista():
    pagina = int(request.args.get('pagina', 1))
    ano = request.args.get('ano')
    por_pagina = 20
    subquery = db.session.query(
        Canal.serie_nome,
        func.min(Canal.id).label('id')
    ).filter(Canal.tipo == 'serie')
    if ano:
        subquery = subquery.filter(Canal.ano_lancamento == ano)
    subquery = subquery.group_by(Canal.serie_nome).subquery()
    query = db.session.query(Canal).join(subquery, Canal.id == subquery.c.id)
    query = filtrar_adultos(query)
    series = query.order_by(Canal.serie_nome).paginate(page=pagina, per_page=por_pagina, error_out=False)
    return jsonify({
        'itens': [s.serialize() for s in series.items],
        'total': series.total,
        'pagina': pagina,
        'total_paginas': series.pages
    })

@app.route('/api/filmes/categorias')
def api_filmes_categorias():
    categorias = db.session.query(Canal.categoria).filter_by(tipo='filme').distinct().all()
    # Filtrar "Adultos" das categorias listadas
    return jsonify([c[0] for c in categorias if c[0] and c[0] != 'Adultos'])

@app.route('/api/series/categorias')
def api_series_categorias():
    categorias = db.session.query(Canal.categoria).filter_by(tipo='serie').distinct().all()
    return jsonify([c[0] for c in categorias if c[0] and c[0] != 'Adultos'])

@app.route('/api/filmes/anos')
def api_filmes_anos():
    anos = db.session.query(Canal.ano_lancamento).filter(
        Canal.tipo == 'filme',
        Canal.ano_lancamento.isnot(None)
    ).distinct().order_by(Canal.ano_lancamento.desc()).all()
    return jsonify([a[0] for a in anos])

@app.route('/api/series/anos')
def api_series_anos():
    anos = db.session.query(Canal.ano_lancamento).filter(
        Canal.tipo == 'serie',
        Canal.ano_lancamento.isnot(None)
    ).distinct().order_by(Canal.ano_lancamento.desc()).all()
    return jsonify([a[0] for a in anos])

@app.route('/api/filmes/categoria/<categoria>/lista')
def api_filmes_categoria_lista(categoria):
    pagina = int(request.args.get('pagina', 1))
    ano = request.args.get('ano')
    por_pagina = 20
    query = Canal.query.filter_by(tipo='filme', categoria=categoria)
    if ano:
        query = query.filter_by(ano_lancamento=ano)
    query = filtrar_adultos(query)
    total = query.count()
    filmes = query.order_by(Canal.nome).paginate(page=pagina, per_page=por_pagina, error_out=False)
    return jsonify({
        'itens': [f.serialize() for f in filmes.items],
        'total': total,
        'pagina': pagina,
        'total_paginas': filmes.pages
    })

@app.route('/api/series/categoria/<categoria>/lista')
def api_series_categoria_lista(categoria):
    pagina = int(request.args.get('pagina', 1))
    ano = request.args.get('ano')
    por_pagina = 20
    subquery = db.session.query(
        Canal.serie_nome,
        func.min(Canal.id).label('id')
    ).filter(Canal.tipo == 'serie', Canal.categoria == categoria)
    if ano:
        subquery = subquery.filter(Canal.ano_lancamento == ano)
    subquery = subquery.group_by(Canal.serie_nome).subquery()
    query = db.session.query(Canal).join(subquery, Canal.id == subquery.c.id)
    query = filtrar_adultos(query)
    series = query.order_by(Canal.serie_nome).paginate(page=pagina, per_page=por_pagina, error_out=False)
    return jsonify({
        'itens': [s.serialize() for s in series.items],
        'total': series.total,
        'pagina': pagina,
        'total_paginas': series.pages
    })

@app.route('/api/busca')
def api_busca():
    termo = request.args.get('q', '').strip()
    pagina = int(request.args.get('pagina', 1))
    por_pagina = 20
    if not termo:
        return jsonify({'itens': [], 'total': 0, 'pagina': 1, 'total_paginas': 1})

    # Agrupar séries (apenas as que correspondem ao termo e não são adultas)
    subquery_series = db.session.query(
        Canal.serie_nome,
        func.min(Canal.id).label('id')
    ).filter(
        Canal.tipo == 'serie',
        Canal.nome.ilike(f'%{termo}%'),
        Canal.categoria != 'Adultos'
    ).group_by(Canal.serie_nome).subquery()

    series = db.session.query(Canal).join(
        subquery_series, Canal.id == subquery_series.c.id
    ).all()

    # Filmes, TV, rádio (filtrando adultos)
    outros = Canal.query.filter(
        Canal.tipo.in_(['filme', 'tv', 'radio']),
        Canal.nome.ilike(f'%{termo}%'),
        Canal.categoria != 'Adultos'
    ).all()

    resultados = series + outros
    resultados.sort(key=lambda x: x.nome)

    total = len(resultados)
    inicio = (pagina - 1) * por_pagina
    fim = inicio + por_pagina
    itens_pagina = resultados[inicio:fim]

    return jsonify({
        'itens': [c.serialize() for c in itens_pagina],
        'total': total,
        'pagina': pagina,
        'total_paginas': (total + por_pagina - 1) // por_pagina
    })

def serialize_canal(canal):
    return {
        'id': canal.id,
        'nome': canal.nome,
        'url': canal.url,
        'logo': canal.logo,
        'tipo': canal.tipo,
        'categoria': canal.categoria,
        'temporada': canal.temporada,
        'episodio': canal.episodio,
        'serie_nome': canal.serie_nome,
        'ano_lancamento': canal.ano_lancamento
    }
Canal.serialize = serialize_canal

# ---------- Favoritos ----------
@app.route('/favoritar/<int:canal_id>', methods=['POST'])
def favoritar(canal_id):
    if 'usuario_id' not in session:
        return jsonify({'erro': 'Não autenticado'}), 401
    usuario_id = session['usuario_id']
    canal = Canal.query.get_or_404(canal_id)
    # Impedir favoritar conteúdo adulto (opcional)
    if canal.categoria == 'Adultos':
        return jsonify({'erro': 'Conteúdo não disponível'}), 403
    existe = Favorito.query.filter_by(usuario_id=usuario_id, canal_id=canal_id).first()
    if existe:
        db.session.delete(existe)
        db.session.commit()
        return jsonify({'status': 'removido'})
    else:
        fav = Favorito(usuario_id=usuario_id, canal_id=canal_id, tipo=canal.tipo)
        db.session.add(fav)
        db.session.commit()
        return jsonify({'status': 'adicionado'})

@app.route('/api/favoritos')
def api_favoritos():
    if 'usuario_id' not in session:
        return jsonify({'erro': 'Não autenticado'}), 401
    usuario_id = session['usuario_id']
    favs = Favorito.query.filter_by(usuario_id=usuario_id).all()
    # Filtrar favoritos adultos (embora não devam existir)
    favs = [f for f in favs if f.canal and f.canal.categoria != 'Adultos']
    return jsonify([f.canal.serialize() for f in favs if f.canal])

# ---------- Progresso ----------
@app.route('/progresso/<int:canal_id>', methods=['POST'])
def salvar_progresso(canal_id):
    if 'usuario_id' not in session:
        return jsonify({'erro': 'Não autenticado'}), 401
    # Verificar se o canal é adulto (não deve salvar progresso)
    canal = Canal.query.get(canal_id)
    if canal and canal.categoria == 'Adultos':
        return jsonify({'erro': 'Conteúdo não disponível'}), 403
    data = request.get_json()
    tempo = data.get('tempo')
    duracao = data.get('duracao')
    usuario_id = session['usuario_id']
    progresso = Progresso.query.filter_by(usuario_id=usuario_id, canal_id=canal_id).first()
    if progresso:
        progresso.tempo = tempo
        progresso.duracao = duracao
        progresso.data_atualizacao = datetime.utcnow()
    else:
        progresso = Progresso(usuario_id=usuario_id, canal_id=canal_id, tempo=tempo, duracao=duracao)
        db.session.add(progresso)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/progresso/<int:canal_id>', methods=['GET'])
def obter_progresso(canal_id):
    if 'usuario_id' not in session:
        return jsonify({'erro': 'Não autenticado'}), 401
    usuario_id = session['usuario_id']
    progresso = Progresso.query.filter_by(usuario_id=usuario_id, canal_id=canal_id).first()
    if progresso:
        return jsonify({'tempo': progresso.tempo, 'duracao': progresso.duracao})
    return jsonify({'tempo': 0, 'duracao': 0})

# ---------- Proxy ----------
@app.route('/proxy')
def proxy():
    url = request.args.get('url')
    if not url:
        return 'URL não fornecida', 400
    headers = {}
    if 'Range' in request.headers:
        headers['Range'] = request.headers.get('Range')
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=10)
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_headers]
        return Response(resp.iter_content(chunk_size=8192), status=resp.status_code, headers=headers)
    except Exception as e:
        return f'Erro no proxy: {str(e)}', 500

# ---------- Criar admin padrão ----------
def criar_admin_padrao():
    if Usuario.query.filter_by(is_admin=True).first() is None:
        hash_senha = generate_password_hash('admin')
        admin = Usuario(
            nome='Administrador',
            email='admin@teste.com',
            senha=hash_senha,
            is_admin=True,
            expira_em=None
        )
        db.session.add(admin)
        db.session.commit()
        logger.info("Usuário admin padrão criado: admin@teste.com / admin")
    else:
        logger.info("Usuário admin já existe.")

if __name__ == '__main__':
    with app.app_context():
        carregar_json_no_banco()
        criar_admin_padrao()
    app.run(debug=True)