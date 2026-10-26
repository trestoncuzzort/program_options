from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root=Path.cwd()
out=root/'validation-output'
out.mkdir()
compiler=os.environ['VALIDATION_COMPILER']
fixed_sha='9884080b11711628a03e20ccdf592f5160d6e079'
original_sha='e173046605e9ef04282135a4470f1eac38e5631f'
records={'scope':'Targeted copy regression and new-header compatibility with pristine/rebuilt static/shared libraries; not the entire upstream CI.',
         'compiler':compiler,'steps':[]}

def save():
    (out/'results.json').write_text(json.dumps(records,indent=2)+'\n')

def dependency(item):
    name=item['repository'].split('/')[-1]
    directory=root/'deps'/name
    directory.mkdir(parents=True)
    with (out/('dependency-'+name+'.log')).open('w') as stream:
        for command in [['git','init',str(directory)],['git','-C',str(directory),'fetch','--depth=1',
                        'https://github.com/'+item['repository']+'.git',item['head']],
                        ['git','-C',str(directory),'checkout','--detach','FETCH_HEAD']]:
            subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True)
    actual=subprocess.check_output(['git','-C',str(directory),'rev-parse','HEAD'],text=True).strip()
    assert actual==item['head'],(item,actual)
    return dict(item,include=str(directory/'include'))

with ThreadPoolExecutor(max_workers=6) as pool:
    records['dependencies']=list(pool.map(dependency,json.loads((root/'validation/dependencies.json').read_text())))
for phase,sha in [('original',original_sha),('fixed',fixed_sha)]:
    actual=subprocess.check_output(['git','-C',str(root/phase),'rev-parse','HEAD'],text=True).strip()
    assert actual==sha,(phase,actual)
records.update(original_sha=original_sha,fixed_sha=fixed_sha)
save()

def execute(command,label,cwd=out,expected=0,env=None):
    start=time.monotonic()
    with (out/(label+'.log')).open('w') as stream:
        p=subprocess.run(list(map(str,command)),cwd=cwd,stdin=subprocess.DEVNULL,
                         stdout=stream,stderr=subprocess.STDOUT,env=env)
    text=(out/(label+'.log')).read_text(errors='replace')
    records['steps'].append(dict(label=label,command=list(map(str,command)),returncode=p.returncode,
                                expected=expected,seconds=time.monotonic()-start,output_tail=text[-2000:]))
    save();print(label,'exit',p.returncode,flush=True)
    if expected=='failure': assert p.returncode!=0,(label,text[-4000:])
    else: assert p.returncode==expected,(label,text[-4000:])
    return text

def flags(phase,shared=False):
    paths=[root/phase/'include',root/'validation/boost-root']+[Path(x['include']) for x in records['dependencies']]
    if compiler=='msvc':
        args=['/nologo','/EHsc','/std:c++14','/Zc:__cplusplus','/permissive-','/MD','/Od','/DBOOST_ALL_NO_LIB']
        args+=['/I'+str(p) for p in paths]
        if shared:args+=['/DBOOST_PROGRAM_OPTIONS_DYN_LINK']
    else:
        args=['-std=c++11','-O0','-g','-DBOOST_ALL_NO_LIB']+['-I'+str(p) for p in paths]
        if compiler=='clang-sanitized':args+=['-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer']
        if shared:args+=['-DBOOST_PROGRAM_OPTIONS_DYN_LINK']
    return args

cxx='cl' if compiler=='msvc' else 'g++-11' if compiler=='gcc11' else 'clang++'
source_names=['cmdline','config_file','options_description','parsers','variables_map','value_semantic',
              'positional_options','utf8_codecvt_facet','convert','winmain','split']
libraries={}
for phase in ['original','fixed']:
    sources=[root/phase/'src'/(n+'.cpp') for n in source_names]
    for linkage in ['static','shared']:
        shared=linkage=='shared';label=phase+'-'+linkage
        work=out/label;work.mkdir()
        if compiler=='msvc':
            library=work/('program_options.dll' if shared else 'program_options.lib')
            command=[cxx]+flags(phase,shared)+['/Fo'+str(work)+os.sep]
            if shared:
                execute(command+['/LD','/Fe:'+str(library)]+sources+
                        ['/link','/IMPLIB:'+str(work/'program_options.lib')],label+'-build',cwd=work)
                link_library=work/'program_options.lib'
            else:
                execute(command+['/c']+sources,label+'-compile',cwd=work)
                objects=sorted(work.glob('*.obj'));assert len(objects)==11,objects
                execute(['lib','/nologo','/OUT:'+str(library)]+objects,label+'-archive',cwd=work)
                link_library=library
        else:
            library=work/('libprogram_options.dylib' if shared and sys.platform=='darwin' else
                          'libprogram_options.so' if shared else 'libprogram_options.a')
            command=[cxx]+flags(phase,shared)
            if shared:
                execute(command+['-fPIC','-dynamiclib' if sys.platform=='darwin' else '-shared']+
                        sources+['-o',library],label+'-build',cwd=work)
            else:
                execute(command+['-c']+sources,label+'-compile',cwd=work)
                objects=sorted(work.glob('*.o'));assert len(objects)==11,objects
                execute(['ar','rcs',library]+objects,label+'-archive',cwd=work)
            link_library=library
        libraries[(phase,linkage)]=(link_library,work)

fixture=root/'fixed/test/copy_test.cpp'
def consumer(header_phase,lib_phase,linkage,label,expected=0):
    library,libdir=libraries[(lib_phase,linkage)]
    exe=out/(label+('.exe' if os.name=='nt' else ''))
    if compiler=='msvc':
        command=[cxx]+flags(header_phase,linkage=='shared')+['/Fo:'+str(out/(label+'.obj')),
                  '/Fe:'+str(exe),fixture,library]
    else:
        command=[cxx]+flags(header_phase,linkage=='shared')+[fixture,library,'-o',exe]
    text=execute(command,label+'-build',expected=expected)
    if expected=='failure':
        assert 'deprecated-copy' in text and ('validation_error' in text or 'abstract_variables_map' in text),text[-4000:]
        return
    env=dict(os.environ)
    for key in ['PATH','LD_LIBRARY_PATH','DYLD_LIBRARY_PATH']:
        env[key]=str(libdir)+os.pathsep+env.get(key,'')
    text=execute([exe],label+'-run',env=env)
    assert 'No errors detected.' in text,text

consumer('original','original','static','original-control',
         expected='failure' if compiler.startswith('clang') else 0)
for phase in ['original','fixed']:
    for linkage in ['static','shared']:
        consumer('fixed',phase,linkage,'fixed-headers-'+phase+'-'+linkage)

paths=['include/boost/program_options/errors.hpp','include/boost/program_options/variables_map.hpp',
       'test/copy_test.cpp','test/Jamfile.v2']
records['source_sha256']={p:hashlib.sha256((root/'fixed'/p).read_bytes()).hexdigest() for p in paths}
records['version_sha256']=hashlib.sha256((root/'validation/boost-root/boost/version.hpp').read_bytes()).hexdigest()
for phase in ['original','fixed']:
    assert not subprocess.check_output(['git','-C',str(root/phase),'status','--porcelain','--untracked-files=no'],text=True)
records['all_checks_passed']=True
save();print('Pinned old diagnostic control and static/shared compatibility checks passed.',flush=True)
