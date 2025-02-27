import logging

import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np

from .protein_utils.rigid import rigid_from_3_points, rot_to_quat, quat_to_axis_angles, axis_angle_to_pos, quat_to_rot, rand_quat, pos_to_affine7, affine6_to_affine7, affine7_to_affine6, affine_to_pos
from .protein_utils.backbone import backbone_frame_to_atom3_std, backbone_fape_loss
from .protein_geom_utils import get_descrete_dist, get_descrete_feature
from .protein_utils.add_o_atoms import add_atom_O
from .protein_utils.symmetry_utils import get_transform, get_rotrans_from_array_4x4
from .protein_utils.symmetry_ops_utils import get_assemly_xt_from_au_xt_in_ori, get_au_centriod_trans, get_assembly_batch_from_au_batch
from .folding_af2 import r3

from .protein_utils.write_pdb import write_multichain_from_atoms
from .diff_evoformeripa import EvoformerIPA


logger = logging.getLogger(__name__)


class DDPM(nn.Module):
    def __init__(self, config, global_config) -> None:
        super().__init__()
        self.config = config
        self.global_config = global_config

        beta_start, beta_end = global_config.diffusion.betas
        T = global_config.diffusion.T
        self.T = T
        betas = torch.linspace(beta_start, beta_end, T, dtype=torch.float32)

        alphas = 1. - betas
        alphas_cumprod = torch.cumprod(alphas, axis=0)
        alphas_cumprod_prev = F.pad(alphas_cumprod[:-1], (1, 0), value = 1.)

        register_buffer = lambda name, val: self.register_buffer(name, val.to(torch.float32))
        register_buffer('betas', betas)
        register_buffer('alphas_cumprod', alphas_cumprod)
        register_buffer('alphas_cumprod_prev', alphas_cumprod_prev)
        register_buffer('sqrt_alphas_cumprod', torch.sqrt(alphas_cumprod))
        register_buffer('sqrt_one_minus_alphas_cumprod', torch.sqrt(1. - alphas_cumprod))

        # Calculations for posterior q(y_{t-1} | y_t, y_0)
        posterior_variance = betas * (1 - alphas_cumprod_prev) / (1 - alphas_cumprod)
        posterior_variance = torch.stack([posterior_variance, torch.FloatTensor([1e-20] * self.T)])
        posterior_log_variance_clipped = posterior_variance.max(dim=0).values.log()
        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        posterior_mean_coef1 = betas * alphas_cumprod_prev.sqrt() / (1 - alphas_cumprod)
        posterior_mean_coef2 = (1 - alphas_cumprod_prev) * alphas.sqrt() / (1 - alphas_cumprod)
        posterior_mean_coef3 = (1 - (betas * alphas_cumprod_prev.sqrt() + alphas.sqrt() * (1 - alphas_cumprod_prev))/ (1 - alphas_cumprod)) # only for mu from prior
        self.register_buffer('posterior_log_variance_clipped', posterior_log_variance_clipped)
        self.register_buffer('posterior_mean_coef1', posterior_mean_coef1)
        self.register_buffer('posterior_mean_coef2', posterior_mean_coef2)
        self.register_buffer('posterior_mean_coef3', posterior_mean_coef3) # only for mu from prior

        self.x0_pred_net = EvoformerIPA(config.diff_model, global_config)

        self.position_scale = 0.0667
        self.affine_scale = torch.FloatTensor([1.0, 1.0, 1.0, 1.0] + [1.0 / self.position_scale] * 3)
        self.affine_tensor_scale = torch.FloatTensor([1.0, 1.0, 1.0] + [1.0 / self.position_scale] * 3)


    def q_sample(self, x0_dict: dict, t, mu_dict=None, condition=None, gt_affine7=None):
        # Calculations for posterior q(x_{t} | x_0, mu)
        xt_dict = {}
        if x0_dict.__contains__('esm'):
            xt_esm = self.degrad_esm(x0_dict['esm'], t, mu_dict if mu_dict is not None else None)
            xt_dict['esm'] = xt_esm

        if x0_dict.__contains__('affine'):
            xt_affine = self.degrad_affine(x0_dict['affine'], t, mu_dict if mu_dict is not None else None, condition, gt_affine7)
            xt_dict['affine'] = xt_affine

        return xt_dict


    def q_sample_symmetry(self, x0_dict: dict, t, init_au_rot, au_length, mu_dict=None, condition=None, gt_affine7=None):
        # Calculations for posterior q(x_{t} | x_0, mu)

        # calculate trans and std_vec
        au_centroid_trans = get_au_centriod_trans(x0_dict['affine'], au_length)
        # import pdb; pdb.set_trace()
        au_affine_in_ori = x0_dict['affine'][..., :au_length, :] - F.pad(au_centroid_trans[0], (4, 0, 0, 0))[:, None]
        au_esm = x0_dict['esm'][..., :au_length, :]
        
        au_xt_dict_in_ori = {}
        if x0_dict.__contains__('esm'):
            au_xt_esm = self.degrad_esm(au_esm, t, mu_dict if mu_dict is not None else None)
            au_xt_dict_in_ori['esm'] = au_xt_esm

        if x0_dict.__contains__('affine'):
            au_xt_affine_in_ori = self.degrad_affine(au_affine_in_ori, t, mu_dict if mu_dict is not None else None, condition, gt_affine7)
            au_xt_dict_in_ori['affine'] = au_xt_affine_in_ori

        # ops au with sym_ops and saved trans to get assembly
        xt_dict = get_assemly_xt_from_au_xt_in_ori(au_xt_dict_in_ori, au_centroid_trans, init_au_rot)

        return xt_dict


    def q_posterior(self, xt_dict, x0_dict, t, mu_dict=None):
        # Calculations for posterior q(x_{t-1} | x_t, x_0, mu)
        q_posterior_dict = {}
        if mu_dict is None:
            mu_dict = {k: 0. for k in xt_dict.keys()}
        for k in xt_dict.keys():
            if k != 'affine':
                posterior_mean = self.posterior_mean_coef1[t] * x0_dict[k] + self.posterior_mean_coef2[t] * xt_dict[k]
                model_log_variance = self.posterior_log_variance_clipped[t]
                posterior_mean = posterior_mean + mu_dict[k] * self.posterior_mean_coef3[t]

                eps = torch.randn_like(posterior_mean) if t > 0 else torch.zeros_like(posterior_mean)
                x_t_1 = posterior_mean + eps * (0.5 * model_log_variance).exp()
                q_posterior_dict[k] = x_t_1
            else:
                x0_affine6 = affine7_to_affine6(x0_dict[k])
                xt_affine6 = affine7_to_affine6(xt_dict[k])
                mu_affine6 = affine7_to_affine6(mu_dict[k])

                posterior_mean = self.posterior_mean_coef1[t] * x0_affine6 + self.posterior_mean_coef2[t] * xt_affine6
                model_log_variance = self.posterior_log_variance_clipped[t]
                posterior_mean = posterior_mean + mu_affine6 * self.posterior_mean_coef3[t]

                eps = torch.randn_like(posterior_mean) if t > 0 else torch.zeros_like(posterior_mean)
                x_t_1 = posterior_mean + eps * (0.5 * model_log_variance).exp() * self.affine_tensor_scale.to(x0_affine6.device)
                q_posterior_dict[k] = affine6_to_affine7(x_t_1)

        return q_posterior_dict

    
    def degrad_esm(self, esm_0, t, prior_dict=None):
        t1 = t[..., None, None]
        if prior_dict is None:
            prior_esm = 0
        else:
            prior_esm = prior_dict['esm']
        noise = torch.randn_like(esm_0)
        degrad_esm = (esm_0 - prior_esm) * self.sqrt_alphas_cumprod[t1] + noise * self.sqrt_one_minus_alphas_cumprod[t1] + prior_esm
        return degrad_esm


    def degrad_affine(self, affine_0, t, prior_dict=None, condition=None, gt_affine7=None):
        device = affine_0.device
        t1 = t[..., None, None]

        affine_tensor = affine7_to_affine6(affine_0)

        if prior_dict is not None:
            prior_affine_tensor = affine7_to_affine6(prior_dict['affine'])
        else:
            prior_affine_tensor = 0

        noise = torch.randn_like(affine_tensor) * self.sqrt_one_minus_alphas_cumprod[t1] * self.affine_tensor_scale.to(device)
        degraded_affine_tensor = (affine_tensor - prior_affine_tensor) * self.sqrt_alphas_cumprod[t1] + noise + prior_affine_tensor

        degraded_affine = affine6_to_affine7(degraded_affine_tensor)

        if condition is not None:
            assert gt_affine7 is not None
            degraded_affine = torch.where(condition[..., None]==1, gt_affine7[..., 0, :], degraded_affine)

        return degraded_affine
    

    def forward(self, batch: dict, mu_dict: dict=None):
        affine_0 = r3.rigids_to_quataffine_m(r3.rigids_from_tensor_flat12(batch['gt_backbone_frame'])).to_tensor()[..., 0, :]
        device = batch['gt_pos'].device
        batch_size = batch['gt_pos'].shape[0]

        t = torch.randint(0, self.T, (batch_size,), device=device).long()
        batch['t'] = t
        x0_dict = {
            'affine': affine_0,
            'esm': batch['norm_esm_single']
        }
        batch['x0_dict'] = x0_dict
     
        if mu_dict is not None:
            xt_dict = self.q_sample(batch['x0_dict'], t, mu_dict, batch['condition'], batch['gt_affine'])
        else:
            xt_dict = self.q_sample(batch['x0_dict'], t)
        batch['xt_dict'] = xt_dict

        pred_dict = self.x0_pred_net(batch)
 
        affine_p = pred_dict['traj']
        losses, _ = self.fape_loss(affine_p, affine_0, batch['gt_pos'][..., :3, :], batch['seq_mask'], batch['condition'])
        if pred_dict.__contains__('esm'):
            esm_loss = self.esm_loss(batch, pred_dict['esm'], batch['norm_esm_single'])
            losses.update(esm_loss)
        if pred_dict.__contains__('distogram'):
            distogram_loss = self.distogram_loss(batch, pred_dict['distogram'])
            losses.update(distogram_loss)

        return losses


    @torch.no_grad()
    def sampling(self, batch: dict, pdb_prefix: str, step_num: int, mu_dict: dict=None, return_traj=False, init_noising_scale=1.0, symmetry: str=None):
        device = batch['aatype'].device
        batch_size, num_res = batch['aatype'].shape[:2]

        if mu_dict is None:
            affine_tensor_nosie = torch.randn((1, num_res, 6), dtype=torch.float32).to(device) * init_noising_scale
            affine_tensor_t = affine_tensor_nosie * self.affine_tensor_scale.to(device)
            affine_t = affine6_to_affine7(affine_tensor_t)

            esm_t = torch.randn(
                (1, num_res, self.global_config.esm_num), dtype=torch.float32).to(device)
        else:
            affine_tensor_noise = torch.randn((1, num_res, 6), dtype=torch.float32).to(device) * init_noising_scale
            affine_tensor_t = affine7_to_affine6(mu_dict['affine']) + affine_tensor_noise * self.affine_tensor_scale.to(device)
            affine_t = affine6_to_affine7(affine_tensor_t)

            # import pdb; pdb.set_trace()
            affine_t = torch.where(batch['condition'][..., None]==1, batch['traj_affine'][..., 0, :], affine_t)

            esm_noise = torch.randn(
                (1, num_res, self.global_config.esm_num), dtype=torch.float32).to(device) * init_noising_scale
            esm_t = mu_dict['esm'] + esm_noise

        xt_dict = {
            'affine': affine_t,
            'esm': esm_t
        }
        # import pdb; pdb.set_trace()
        if symmetry is not None:
            init_au_rot, init_au_trans = get_rotrans_from_array_4x4(get_transform(symmetry))
            init_au_rot = torch.from_numpy(init_au_rot).to(device)
            init_au_trans = torch.from_numpy(init_au_trans).to(device)

            xt_dict = get_assemly_xt_from_au_xt_in_ori(xt_dict, init_au_trans, init_au_rot)
            au_length = num_res
            au_num = init_au_rot.shape[0]
            # import pdb; pdb.set_trace()
            # debug_mu_dict = get_assemly_xt_from_au_xt_in_ori(mu_dict, init_au_trans, init_au_rot)
            # debug_mu_coord = self.affine_to_coord(debug_mu_dict['affine'])
            # debug_mu_coord_o = [add_atom_O(debug_mu_coord[0, au_idx * au_length: (au_idx + 1) * au_length].detach().cpu().numpy()[..., :3, :]).reshape(-1, 3) for au_idx in range(au_num)]
            # write_multichain_from_atoms(debug_mu_coord_o, f'/train14/superbrain/yfliu25/structure_refine/debug_PriorDiff_evo2_fixaffine_fixfape_condition/trash/debug_multimeer_orientation.pdb', natom=4)
            # import pdb; pdb.set_trace()
            get_assembly_batch_from_au_batch(batch, au_num)

        batch['xt_dict'] = xt_dict
        
        if not batch.__contains__('gt_pos'):
            batch['gt_pos'] = mu_dict['coord']
            affine_0 = mu_dict['affine']
        else:
            affine_0 = r3.rigids_to_quataffine_m(
                r3.rigids_from_tensor_flat12(batch['gt_backbone_frame'])
                ).to_tensor()[..., 0, :]
        
        t_scheme = ((np.exp(np.arange(100) * 0.02) - np.exp(np.arange(100) * 0.02)[0])/np.exp(np.arange(100) * 0.02)[-1] * 400).astype(np.int32)[::-1]
        # t_scheme = range(self.T-1, -1, -step_num)
        for t in t_scheme:
            t = torch.LongTensor([t] * batch_size).to(device)
            batch['t'] = t
            # import pdb; pdb.set_trace()
            # write_multichain_from_atoms([add_atom_O(self.affine_to_coord(batch['xt_dict']['affine']).detach().cpu().numpy()[..., :3, :]).reshape(-1, 3)], f'/train14/superbrain/yfliu25/structure_refine/debug_PriorDiff_evo2_fixaffine_fixfape_condition/trash/{t[0].item()}_xt.pdb', natom=4)

            x0_dict = self.x0_pred_net(batch)
            x0_dict = {k: v[-1] if k == 'traj' else v for k, v in x0_dict.items()}
            x0_dict['affine'] = x0_dict['traj']

            # generate traj and logger
            affine_p = x0_dict['traj']
            # import pdb; pdb.set_trace()
            losses, pred_x0_dict = self.fape_loss(
                affine_p[:, :num_res][None], affine_0, 
                batch['gt_pos'][..., :3, :], batch['seq_mask'][:, :num_res], 
                batch['condition'][:, :num_res])
            fape_loss = losses['fape_loss'].item()
            clamp_fape = losses['clamp_fape_loss'].item()
            logger.info(f'step: {t[0].item()}/{self.T}; fape loss: {round(fape_loss, 3)}; clamp fape: {round(clamp_fape, 3)}')

            if return_traj:
                for batch_idx in range(batch_size):
                    if symmetry is None:
                        traj_coord_0 = add_atom_O(pred_x0_dict['coord'][0, batch_idx].detach().cpu().numpy()[..., :3, :])
                        write_multichain_from_atoms([
                            traj_coord_0.reshape(-1, 3)], 
                            f'{pdb_prefix}_diff_{t[0].item()}_batch_{batch_idx}.pdb', natom=4)
                    else:
                        multichain_coord = self.affine_to_coord(affine_p)
                        # import pdb; pdb.set_trace()
                        traj_coord_0 = [add_atom_O(multichain_coord[0, au_idx * au_length: (au_idx + 1) * au_length].detach().cpu().numpy()[..., :3, :]).reshape(-1, 3) for au_idx in range(au_num)]
                        write_multichain_from_atoms(traj_coord_0, f'{pdb_prefix}_diff_{t[0].item()}_batch_{batch_idx}.pdb', natom=4)

                if t[0] == ((self.T-1) % step_num):
                    esm1b_dict = {
                        'gt_aatype': batch['aatype'],
                        'esm1b': x0_dict['esm'].detach().cpu().numpy()
                    }
                    np.save(f'{pdb_prefix}_esm1b_pred.npy', esm1b_dict)
                    logger.info('esm1b prediction saved')

            if symmetry is None:
                x_t_1_dict = self.q_sample(x0_dict, t, mu_dict, batch['condition'], affine_0[..., None, :])
            else:
                # ops au in origin with mu
                x_t_1_dict = self.q_sample_symmetry(x0_dict, t, init_au_rot, au_length, mu_dict, batch['condition'][:, :num_res], affine_0[..., None, :])

            batch['xt_dict'] = x_t_1_dict

        x0_dict = batch['xt_dict']

        return x0_dict
    
    
    def fape_loss(self, affine_p, affine_0, coord_0, mask, cond=None):
        quat_0 = affine_0[..., :4]
        trans_0 = affine_0[..., 4:]
        rot_0 = quat_to_rot(quat_0)

        batch_size, num_res = affine_0.shape[:2]

        rot_list, trans_list, coord_list = [], [], []
        num_ouputs = affine_p.shape[0]
        loss_unclamp, loss_clamp = [], []
        for i in range(num_ouputs):
            quat = affine_p[i, ..., :4]
            trans = affine_p[i, ..., 4:]
            rot = quat_to_rot(quat)
            coord = backbone_frame_to_atom3_std(
                torch.reshape(rot, (-1, 3, 3)),
                torch.reshape(trans, (-1, 3)),
            )
            # import pdb; pdb.set_trace()
            coord = torch.reshape(coord, (batch_size, num_res, 3, 3))
            coord_list.append(coord)
            rot_list.append(rot),
            trans_list.append(trans)

            if cond is not None:
                mask_2d = mask[..., None] * mask[..., None, :]
                affine_p_ = affine_p[i]
                coord_p_ = coord
            else:
                mask_2d = 1 - (cond[..., None] * cond[..., None, :])
                mask_2d = mask_2d * (mask[..., None] * mask[..., None, :])   
                # import pdb; pdb.set_trace()
                affine_p_ = torch.where(cond[..., None] == 1, affine_0, affine_p[i])
                coord_p_ = torch.where(cond[..., None, None] == 1, coord_0[:, :, :3], coord)

            quat_p_ = affine_p_[..., :4]
            trans_p_ = affine_p_[..., 4:]
            rot_p_ = quat_to_rot(quat_p_)

            fape, fape_clamp = backbone_fape_loss(
                coord_p_, rot_p_, trans_p_,
                coord_0, rot_0, trans_0, mask,
                clamp_dist=self.global_config.fape.clamp_distance,
                length_scale=self.global_config.fape.loss_unit_distance,
                mask_2d=mask_2d
            )
            loss_unclamp.append(fape)
            loss_clamp.append(fape_clamp)
        
        loss_unclamp = torch.stack(loss_unclamp)
        loss_clamp = torch.stack(loss_clamp)

        clamp_weight = self.global_config.fape.clamp_weight
        loss = loss_unclamp * (1.0 - clamp_weight) + loss_clamp * clamp_weight
        
        last_loss = loss[-1]
        traj_loss = loss.mean()

        traj_weight = self.global_config.fape.traj_weight
        loss = last_loss + traj_weight * traj_loss

        losses = {
            'fape_loss': loss,
            'clamp_fape_loss': loss_clamp[-1],
            'unclamp_fape_loss': loss_unclamp[-1],
            'last_loss': last_loss,
            'traj_loss': traj_loss,
        }
        coord_dict = {
            'coord': torch.stack(coord_list),
            'rot': torch.stack(rot_list),
            'trans': torch.stack(trans_list)
        }
        return losses, coord_dict


    def esm_loss(self, batch, pred_esm, true_esm):
        esm_loss_dict = {}
        esm_single_error = F.mse_loss(pred_esm, true_esm, reduction='none') # B, L, D
        esm_single_mask = (batch['seq_mask'] * batch['esm_single_mask'])[..., None].repeat(1,1,self.global_config.esm_num)
        esm_single_error = esm_single_error * esm_single_mask
        esm_loss_dict['esm_single_pred_loss'] = torch.sum(esm_single_error) / (torch.sum(esm_single_mask) + 1e-6)
        return esm_loss_dict


    def distogram_loss(self, batch, pred_maps_descrete):
        batchsize, nres = pred_maps_descrete.shape[:2]
        distogram_list = []
        distogram_pred_config = self.config.diff_model.distogram_pred
        if distogram_pred_config.pred_all_dist:
            if distogram_pred_config.atom3_dist:
                dist_type_name = ['ca-ca', 'n-n', 'c-c', 'ca-n', 'ca-c', 'n-c']
            else:
                if distogram_pred_config.ca_dist:
                    dist_type_name = ['ca-ca']
                else:
                    dist_type_name = ['cb-cb']

            for dist_type_idx, dist_type in enumerate(dist_type_name):
                gt_map_descrete = get_descrete_dist(batch['gt_pos'], dist_type, distogram_pred_config.distogram_args)
                dim_start = (dist_type_idx) * distogram_pred_config.distogram_args[-1]
                dim_end = (dist_type_idx + 1) * distogram_pred_config.distogram_args[-1]
                pred_map = pred_maps_descrete[..., dim_start: dim_end]
                distogram_loss = F.cross_entropy(
                    pred_map.reshape(-1, distogram_pred_config.distogram_args[-1]), 
                    gt_map_descrete.reshape(-1), reduction='none'
                    ).reshape(batchsize, nres, nres)
                distogram_list.append(distogram_loss)
        
        else:
            descrete_pair, all_angle_masks = get_descrete_feature(
                batch['gt_pos'][..., :4, :], return_angle_mask=True, mask_base_ca=False)
            gt_descrete_pair = descrete_pair[..., 1:].long() # remove ca dist map

            for pair_idx in range(4):
                if pair_idx in [0, 1, 2]:
                    bin_num = distogram_pred_config.distogram_args[-1]
                    dim_start = (pair_idx) * distogram_pred_config.distogram_args[-1]
                    dim_end = (pair_idx + 1) * distogram_pred_config.distogram_args[-1]
                    pred_map = pred_maps_descrete[..., dim_start: dim_end]
                else:
                    bin_num = distogram_pred_config.distogram_args[-1]//2
                    pred_map = pred_maps_descrete[..., -bin_num:]
                distogram_loss = F.cross_entropy(
                    pred_map.reshape(-1, bin_num), 
                    gt_descrete_pair[..., pair_idx].reshape(-1), reduction='none'
                    ).reshape(batchsize, nres, nres)

                if pair_idx in [1, 2, 3]:
                    distogram_loss = distogram_loss * all_angle_masks
                distogram_list.append(distogram_loss)
        # import pdb; pdb.set_trace()
        distogram_loss = torch.stack(distogram_list).mean(0)
        distogram_loss = distogram_loss * batch['pair_mask']
        distogram_loss_reduce = torch.sum(distogram_loss) / (torch.sum(batch['pair_mask']) + 1e-6)
        return {"ditogram_classify_loss": distogram_loss_reduce}



    def affine_to_coord(self, affine):
        batch_size, num_res = affine.shape[:2]
        quat = affine[..., :4]
        trans = affine[..., 4:]
        rot = quat_to_rot(quat)
        coord = backbone_frame_to_atom3_std(
            torch.reshape(rot, (-1, 3, 3)),
            torch.reshape(trans, (-1, 3)),
        )
        coord = torch.reshape(coord, (batch_size, num_res, 3, 3))
        return coord