import copy
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

path = Path(__file__).resolve().parents[1] / 'dot_local/bin/executable_bb-worker-cert'
loader = importlib.machinery.SourceFileLoader('worker_cert', str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)

def ca(key=None, days=90):
    key = key or ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test CA')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=2))
            .not_valid_after(now+timedelta(days=days)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .sign(key, hashes.SHA256()))
    return key, cert

class Certificates(unittest.TestCase):
    def setUp(self):
        self.key, self.request = w.new_request('sample', 'worker-1')
        self.ca_key, self.ca = ca()
        _, self.server_ca = ca()
        self.response = w.issue(self.request, w.pem(self.ca), w.private_pem(self.ca_key), w.pem(self.server_ca), 30)
        self.pin = w.fingerprint(self.ca)

    def validate(self, response=None, request=None, key=None, pin=None):
        return w.validate_response(response or self.response, request or self.request, key or self.key, pin or self.pin)

    def test_valid_round_trip_and_no_private_material(self):
        self.validate(w.unpack(w.pack(self.response, 'RESPONSE'), 'RESPONSE'))
        self.assertNotIn('PRIVATE KEY', w.canonical(self.response).decode())
        cert = x509.load_pem_x509_certificate(self.response['certificate'].encode())
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value), [ExtendedKeyUsageOID.CLIENT_AUTH])

    def test_wrong_signer_rejected(self):
        _, other = ca()
        with self.assertRaises(ValueError): self.validate(pin=w.fingerprint(other))

    def test_request_replay_rejected(self):
        _, other = w.new_request('sample', 'worker-1')
        with self.assertRaises(ValueError): self.validate(request=other)

    def test_wrong_private_key_rejected(self):
        with self.assertRaises(ValueError): self.validate(key=ec.generate_private_key(ec.SECP256R1()))

    def test_server_trust_tampering_rejected(self):
        response = copy.deepcopy(self.response)
        _, other = ca()
        response['server_ca'] = w.pem(other)
        with self.assertRaises(ValueError): self.validate(response=response)

    def test_signed_request_binding_rejected(self):
        response = copy.deepcopy(self.response)
        request = dict(self.request, request_id='a'*64)
        response['request_id'] = request['request_id']
        with self.assertRaises(ValueError): self.validate(response=response, request=request)

    def test_expired_ca_rejected(self):
        key, cert = ca(days=-1)
        with self.assertRaises(ValueError): w.issue(self.request, w.pem(cert), w.private_pem(key), w.pem(self.server_ca), 30)

    def test_mismatched_signing_key_rejected(self):
        key, _ = ca()
        with self.assertRaises(ValueError): w.issue(self.request, w.pem(self.ca), w.private_pem(key), w.pem(self.server_ca), 30)

    def test_lifetime_capped_at_ca_expiration(self):
        key, cert = ca(days=2)
        response = w.issue(self.request, w.pem(cert), w.private_pem(key), w.pem(self.server_ca), 30)
        leaf = x509.load_pem_x509_certificate(response['certificate'].encode())
        self.assertLessEqual(leaf.not_valid_after_utc, cert.not_valid_after_utc)

    def test_invalid_duration_rejected(self):
        for days in (0, 366, True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                w.issue(self.request, w.pem(self.ca), w.private_pem(self.ca_key), w.pem(self.server_ca), days)

    def test_path_and_shell_injection_rejected(self):
        for name in ('../bad', '-oProxyCommand=evil', 'host;id', 'x\ny', ''):
            with self.subTest(name=name), self.assertRaises(ValueError): w.safe_name(name)
        self.assertEqual(w.ssh_command('user@worker', 'gateway')[:5], ['ssh', '-J', 'gateway', '--', 'user@worker'])
        with self.assertRaises(ValueError): w.ssh_command('-oProxyCommand=evil', None)

    def test_bad_bundle_rejected(self):
        for value in ('hello', 'x'*70000, w.pack({'version':2}, 'RESPONSE')):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError): w.unpack(value, 'RESPONSE')

    def test_altered_certificate_signature_rejected(self):
        response = copy.deepcopy(self.response)
        cert = x509.load_pem_x509_certificate(response['certificate'].encode())
        from cryptography.hazmat.primitives import serialization
        der = bytearray(cert.public_bytes(serialization.Encoding.DER))
        der[-1] ^= 1
        response['certificate'] = w.pem(x509.load_der_x509_certificate(bytes(der)))
        with self.assertRaises(ValueError): self.validate(response=response)

    def test_csr_privilege_extension_rejected(self):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'worker-1')])
        csr = (x509.CertificateSigningRequestBuilder().subject_name(subject)
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
               .sign(self.key, hashes.SHA256()))
        from cryptography.hazmat.primitives import serialization
        request = dict(self.request, csr=csr.public_bytes(serialization.Encoding.PEM).decode())
        with self.assertRaises(ValueError):
            w.issue(request, w.pem(self.ca), w.private_pem(self.ca_key), w.pem(self.server_ca), 30)

    def test_install_rejects_response_before_changing_active_files(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state/'request.json').write_bytes(w.canonical(self.request))
            (state/'pending.key').write_text(w.private_pem(self.key))
            (state/'client.key').write_text('existing key')
            with mock.patch.object(w, 'CONFIG', state/'config'), mock.patch.object(w, 'memory_directory', return_value=state), mock.patch.object(w.subprocess, 'run') as run:
                with self.assertRaises(ValueError): w.install_locked(self.response, 'sample', '0'*64, state)
                self.assertEqual((state/'client.key').read_text(), 'existing key')
                run.assert_not_called()

    def test_install_consumes_request_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state/'request.json').write_bytes(w.canonical(self.request))
            (state/'pending.key').write_text(w.private_pem(self.key))
            with mock.patch.object(w, 'CONFIG', state/'config'), mock.patch.object(w, 'memory_directory', return_value=state), mock.patch('builtins.print'):
                w.install_locked(self.response, 'sample', self.pin, state)
                self.assertFalse((state/'pending.key').exists())
                self.assertFalse((state/'request.json').exists())
                self.assertEqual((state/'client.key').stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileNotFoundError): w.install_locked(self.response, 'sample', self.pin, state)

    def test_pending_request_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            first = w.prepare_locked(state, 'sample', 'worker-1')
            self.assertEqual(first, w.prepare_locked(state, 'sample', 'worker-1'))
            with self.assertRaises(ValueError): w.prepare_locked(state, 'sample', 'worker-2')

    def test_connection_check_requires_expected_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'host-id').write_text('expected')
            (root/'host-daemon-port').write_text('38888')
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"hostId":"other", "connected":true}'
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(w.urllib.request, 'build_opener', return_value=opener), mock.patch.object(w.time, 'sleep'), mock.patch.object(w.time, 'monotonic', side_effect=[0, 0, 31]):
                with self.assertRaises(ValueError): w.wait_connected(root/'config.json')

    def test_signing_fields_fetched_in_one_memory_only_operation(self):
        config = dict(client_ca_ref='op://vault/ca/certificate', client_key_ref='op://vault/ca/private_key', server_ca_ref='op://vault/server/certificate')
        def inject(command, **kwargs):
            self.assertEqual(command, ['op', 'inject'])
            text = kwargs['input']
            for ref, value in zip(config.values(), ['PUBLIC', 'PRIVATE', 'TRUST']):
                text = text.replace('{{ '+ref+' }}', value)
            return mock.Mock(returncode=0, stdout=text)
        with mock.patch.object(w.subprocess, 'run', side_effect=inject) as run:
            self.assertEqual(w.signing_material(config), ['PUBLIC', 'PRIVATE', 'TRUST'])
            run.assert_called_once()

    def test_injection_template_in_reference_rejected_before_read(self):
        config = dict(client_ca_ref='op://vault/{{evil}}/certificate', client_key_ref='op://vault/ca/key', server_ca_ref='op://vault/server/cert')
        with mock.patch.object(w.subprocess, 'run') as run:
            with self.assertRaises(ValueError): w.signing_material(config)
            run.assert_not_called()

    def test_malformed_request_rejected_before_vault_access(self):
        request = dict(self.request, csr='not a CSR')
        with mock.patch.object(w, 'load_profile', return_value={}), mock.patch.object(w, 'signing_material') as read:
            with self.assertRaises(ValueError): w.sign(request, 'sample')
            read.assert_not_called()

    def test_symlink_write_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'target'
            target.write_text('unchanged')
            link = Path(directory)/'link'
            link.symlink_to(target)
            with self.assertRaises(ValueError): w.atomic_write(link, b'changed')
            self.assertEqual(target.read_text(), 'unchanged')

if __name__ == '__main__': unittest.main()
