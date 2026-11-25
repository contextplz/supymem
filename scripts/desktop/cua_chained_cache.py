"""Explicit pinned357→700 dependency provenance; no mutable receipt authority."""
from pathlib import Path
from types import SimpleNamespace
import json

REF = '7d1ed53c38a15578b6a3840e7c712543b441df07'
BUILDER = '097783c7478dd4452e9682a35a5a8d2e9f99dc9a452edd47a1487fb1f53fe2d9'
PATCH = '70034a2a2c525a876919f5a42e9c63099e447c9f0e5b44daf8702abf0193ba10'
PREPARED = 'e74d593f524c0cbcdcf03d1f0464eb95dc7ec19dfb55b51786103ff1b3948aac'
RECEIPTS = {
    'source-prepared.json': '718da6312052d70a4067c5e165d4af905996f556a7d8832c737d5e2c104ffac6',
    'native-prepared.json': '569277d6a3cdb55ac360252985b1d776a83b2a8c2dd8b1a0c8055eef33c6115d',
    'candidate.json': 'a00ebd5ecbd4fe0923ce82ab1732bfc20a82cf7fc6394db37bb12a3668bae239',
    'signed-qa-artifacts.json': '9094c7554298e6d0190ccfa5efbaffe7f9b0a12e7cc72df01835a11fe23f354c',
    'dependency-cache-admission.json': 'f1fe653e54d9dc9011aa2a7a7a2b77e5f07635df14cb1a1fe6c7f38d0bb1fb74',
}
UNSIGNED = {'cua-driver':'58c011c1f0691e4a5f195ebff63a0f30be60c93e810a9a83371bc0b4c5c9b630',
            'cua-cursor-theme':'d378c7c75a701a1d3b23f44ec55d18ad082ca36dbe1c9fc381286626a1ca1ca9'}
SIGNED = {'cua-driver':'505b48097c0c654899cb4878ed2bd1e1c7c6ce0e3cc6279d7caec8e15789dece',
          'cua-cursor-theme':'df91a63501185b2c2460ed5571845f7af35e52ad4055eae1f5f5c7e3f503c799'}


def namespace(builder):
    return SimpleNamespace(**builder) if isinstance(builder, dict) else builder


def files(builder):
    b = namespace(builder)
    repo = Path(b.__file__).resolve().parents[2]
    def read(name):
        return b.subprocess.run(['git','show',f'{REF}:{name}'],cwd=repo,check=True,
                                capture_output=True,timeout=30).stdout
    prefix = 'scripts/desktop/vendor/cua-modal-0.29.1/'
    pin = json.loads(read(prefix+'manifest.json'))
    patch = read(prefix+'exact-modal.patch')
    if (b.digest(patch) != PATCH or pin['patch_sha256'] != PATCH
            or b.digest(read('scripts/desktop/build_cua_modal_candidate.py')) != BUILDER
            or b.digest(read(prefix+'LICENSE.upstream.md')) != pin['license_sha256']):
        raise ValueError('cua_chained_cache_pin_mismatch')
    return pin,patch


def disjoint(output, cache, chain):
    roots = [output.resolve(),cache.resolve(),chain.resolve()]
    if any(a == c or a.is_relative_to(c) or c.is_relative_to(a)
           for i,a in enumerate(roots) for c in roots[i+1:]):
        raise ValueError('cua_chained_cache_alias_refused')


def compiler_environment(builder):
    b = namespace(builder)
    # These survive the existing common flags/wrapper sanitation and could
    # change Cargo's compiler, target, linker, or test runner. No values leak.
    exact = {'RUSTC','CARGO_BUILD_RUSTC','CARGO_BUILD_TARGET','CARGO_BUILD_RUSTFLAGS',
             'CARGO_BUILD_RUSTC_WRAPPER','CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER'}
    for key,value in b.os.environ.items():
        target_override = key.startswith('CARGO_TARGET_') and key.endswith(('_RUSTFLAGS','_LINKER','_RUNNER'))
        if value and (key in exact or target_override):
            raise ValueError('cua_chained_cache_compiler_override_refused')


def facts(builder, workspace, output, cache, chain):
    b = namespace(builder)
    compiler_environment(b)
    b.private_owned_tree(chain)
    b._dependency_cache_idle(chain)
    disjoint(output,cache,chain)
    if any((chain/name).exists() for name in ('reviewed-build.json','native-trial-receipt.json')):
        raise ValueError('cua_chained_cache_activated_refused')
    pin,_ = files(b)
    preserved = cache/'preserved-signed-root357'
    old = b._dependency_cache_facts(workspace,output,cache,preserved=preserved)
    receipts = {}
    for name,digest in RECEIPTS.items():
        data = b._dependency_metadata_bytes(chain/name)
        if b.digest(data) != digest:
            raise ValueError('cua_chained_cache_receipt_mismatch')
        receipts[name] = json.loads(data)
    if (b.tree_digest(output/'chained-source-proof') != PREPARED
            or b.tree_digest(chain/'source') != PREPARED
            or b.digest((chain/'source/Cargo.lock').read_bytes()) != old['cargo_lock_sha256']
            or b.digest((chain/'LICENSE.upstream.md').read_bytes()) != old['license_sha256']
            or any(pin[k] != receipts['source-prepared.json'].get(k) for k in pin)
            or b._workspace_packages(chain/'source') != old['first_party_packages_to_clean']):
        raise ValueError('cua_chained_cache_source_mismatch')
    prior = receipts['dependency-cache-admission.json']
    if (prior.get('target_path_sha256') != old['target_path_sha256']
            or prior.get('current_prepared_tree_sha256') != PREPARED
            or prior.get('old_prepared_tree_sha256') != old['old_prepared_tree_sha256']
            or prior.get('compiler_sha256') != old['compiler_sha256']
            or prior.get('cargo_lock_sha256') != old['cargo_lock_sha256']
            or prior.get('build_profile') != b.BUILD_PROFILE
            or prior.get('acceptance_inherited') is not False
            or prior.get('reuse_only_dependencies') is not True):
        raise ValueError('cua_chained_cache_chain_mismatch')
    candidate = receipts['candidate.json']
    signed = receipts['signed-qa-artifacts.json']
    native = receipts['native-prepared.json']
    if (candidate.get('binaries_sha256') != UNSIGNED
            or signed.get('unsigned_candidate_manifest_sha256') != RECEIPTS['candidate.json']
            or signed.get('installed_acceptance') is not False or signed.get('activation_allowed') is not False
            or native.get('prepared_tree_sha256') != PREPARED or native.get('cache_only') is not True
            or native.get('compiler_sha256') != old['compiler_sha256']
            or native.get('build_profile') != b.BUILD_PROFILE):
        raise ValueError('cua_chained_cache_attestation_mismatch')
    artifacts = signed.get('artifacts')
    if type(artifacts) is not dict or set(artifacts) != set(SIGNED):
        raise ValueError('cua_chained_cache_signature_mismatch')
    for name, digest in SIGNED.items():
        artifact = chain/'target/release'/name
        unsigned = cache/'target/release'/name
        row = artifacts[name]
        if (artifact.is_symlink() or unsigned.is_symlink()
                or b.digest(artifact.read_bytes()) != digest
                or b.digest(unsigned.read_bytes()) != UNSIGNED[name]
                or row.get('sha256') != digest or row.get('unsigned_sha256') != UNSIGNED[name]
                or row.get('strict_verified') is not True or row.get('team_id') != b.BUILT_CACHE_TEAM):
            raise ValueError('cua_chained_cache_signature_mismatch')
        verified = b.subprocess.run(['/usr/bin/codesign','--verify','--strict',str(artifact)],
                                    capture_output=True,timeout=15)
        identity = b.subprocess.run(['/usr/bin/codesign','-dv','--verbose=2',str(artifact)],
                                    capture_output=True,text=True,timeout=15)
        if (verified.returncode or identity.returncode
                or f'TeamIdentifier={b.BUILT_CACHE_TEAM}' not in identity.stderr.splitlines()):
            raise ValueError('cua_chained_cache_signature_mismatch')
    return {**old,'schema':'supymem_cua_chained_dependency_cache_admission_v1',
            'chain_output':str(chain.resolve()),'chain_ref':REF,'chain_builder_sha256':BUILDER,
            'chain_receipts_sha256':dict(RECEIPTS),'chain_prepared_tree_sha256':PREPARED,
            'chain_unsigned_binaries_sha256':dict(UNSIGNED),'chain_signed_binaries_sha256':dict(SIGNED)}


def validate(builder, pristine, workspace, output, cache, chain):
    b = namespace(builder)
    compiler_environment(b)
    disjoint(output,cache,chain)
    b.private_owned_tree(chain)
    b._dependency_cache_idle(chain)
    b.private_owned_tree(cache)
    b._dependency_cache_idle(cache)
    old_pin,old_patch = b._built_cache_files()
    pin,patch = files(b)
    if b.tree_digest(pristine) != pin['source_tree_sha256'] or old_pin['source_tree_sha256'] != pin['source_tree_sha256']:
        raise ValueError('cua_chained_cache_pristine_mismatch')
    for name,data in [('dependency-source-proof',old_patch),('chained-source-proof',patch)]:
        proof = output/name
        if proof.exists() or proof.is_symlink():
            raise ValueError('cua_dependency_cache_proof_must_be_fresh')
        b.shutil.copytree(pristine,proof)
        patch_path = output/(name+'.patch')
        patch_path.write_bytes(data)
        b.subprocess.run(['git','apply','--check',str(patch_path)],cwd=proof,check=True,timeout=30)
        b.subprocess.run(['git','apply',str(patch_path)],cwd=proof,check=True,timeout=30)
    receipt = facts(b,workspace,output,cache,chain)
    (output/'dependency-cache-admission.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return cache/'target'
